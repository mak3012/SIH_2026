"""Rule-based verifier: every number, depth and date in an answer must match a value in the
evidence that cites it.

The answer is cut into claims: the text up to a run of citation markers ([E1] or [E1][E4]) is
cited by that run; text after the last run is uncited. Per claim:
1. an unknown citation id is a problem;
2. names that contain digits (wells such as SA-07, document titles, event ids) are removed;
3. dates (2021-03-04, 4 March 2021, 4 Mar 2021, March 2021) must equal a date in the cited
   evidence (day, or month and year when no day is written);
4. casing sizes (9-5/8, 13 3/8) become decimals; then every remaining number must equal a cited
   value at the precision written (2,345.1 matches 2345.131). A depth written in ft/feet is
   also accepted when it converts to a cited metre value; a percentage may match a fraction;
5. uncited text with a number or date is a problem.

Cited evidence includes what a cited aggregate was computed from. Values are the item's
numeric fields and the numbers and dates printed in its text.
"""

import re
from datetime import date, datetime
from typing import Any

from api.assistant.evidence import Evidence, EvidenceSet

CITE = re.compile(r"\[(E\d+)\]")
NUMBER = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?(?![\w])")
FRACTION = re.compile(r"(?<![\w.])(\d{1,2})[-\s](\d)/(\d{1,2})(?![\w])")
MONTHS = {
    m: i + 1
    for i, m in enumerate(
        [
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ]
    )
}
MON = "|".join(sorted({*MONTHS, *(m[:3] for m in MONTHS), "sept"}, key=len, reverse=True))
DATE_PATTERNS = [
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})(?:T[\d:.+Z-]*)?"), "ymd"),
    (
        re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?[\s.-]+({MON})\.?[\s.,-]+(\d{{4}})\b", re.I),
        "dmy",
    ),
    (re.compile(rf"\b({MON})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", re.I), "mdy"),
    (re.compile(rf"\b({MON})\.?\s+(\d{{4}})\b", re.I), "my"),
    (re.compile(r"\b(\d{1,2})[./](\d{1,2})[./](\d{4})\b"), "dmy_num"),
]
SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\[(\"'])|\n+")
FT = 0.3048


def _month(s: str) -> int:
    s = s.lower().rstrip(".")
    return MONTHS.get(s) or next(v for k, v in MONTHS.items() if k.startswith(s[:3]))


def dates_in(text: str) -> tuple[list[tuple[int, int, int | None]], str]:
    """Dates found and the text with them blanked out."""
    found: list[tuple[int, int, int | None]] = []

    def take(kind: str) -> Any:
        def f(m: re.Match[str]) -> str:
            g = m.groups()
            try:
                if kind == "ymd":
                    d = (int(g[0]), int(g[1]), int(g[2]))
                elif kind == "dmy":
                    d = (int(g[2]), _month(g[1]), int(g[0]))
                elif kind == "mdy":
                    d = (int(g[2]), _month(g[0]), int(g[1]))
                elif kind == "dmy_num":
                    d = (int(g[2]), int(g[1]), int(g[0]))
                else:
                    found.append((int(g[1]), _month(g[0]), None))
                    return " "
                date(*d)  # validates
                found.append(d)
            except (ValueError, StopIteration):
                return m.group(0)
            return " "

        return f

    for pat, kind in DATE_PATTERNS:
        text = pat.sub(take(kind), text)
    return found, text


def _num(tok: str) -> float:
    return float(tok.replace(",", ""))


def matches(token: str, vals: list[float]) -> bool:
    x = abs(_num(token))
    decimals = len(token.split(".")[1]) if "." in token else 0
    tol = 0.5 * 10 ** (-decimals) + 1e-9
    return any(abs(v - x) <= tol for v in vals)


def facts(items: list[Evidence]) -> tuple[list[float], set[tuple[int, int, int | None]]]:
    nums: list[float] = []
    dts: set[tuple[int, int, int | None]] = set()

    def walk(v: Any) -> None:
        if isinstance(v, bool) or v is None:
            return
        if isinstance(v, datetime | date):
            dts.add((v.year, v.month, v.day))
        elif isinstance(v, int | float):
            nums.append(abs(float(v)))
        elif isinstance(v, str):
            ds, rest = dates_in(v)
            dts.update(ds)
            nums.extend(abs(_num(t)) for t in NUMBER.findall(rest))
            for a, b, c in FRACTION.findall(v):
                nums.append(int(a) + int(b) / int(c))
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list | tuple):
            for x in v:
                walk(x)
        else:  # Decimal and other numerics
            try:
                nums.append(abs(float(v)))
            except (TypeError, ValueError):
                pass

    for e in items:
        walk(e.values)
        walk(e.text)
    nums += [v * 100 for v in list(nums) if 0 <= v <= 1]  # fractions written as percent
    return nums, dts


def split_sentences(text: str) -> list[str]:
    return [s for s in SENTENCE.split(text.strip()) if s.strip()]


CLAIM = re.compile(r"(.*?)((?:\s*\[E\d+\])+)", re.S)


def claims(answer: str) -> list[tuple[str, list[str]]]:
    """(text, cited ids): each run of citation markers covers the text since the previous run,
    so a quoted passage with its own full stops stays one claim; trailing text is uncited."""
    out, end = [], 0
    for m in CLAIM.finditer(answer):
        out.append((m.group(1), CITE.findall(m.group(2))))
        end = m.end()
    tail = answer[end:]
    out += [(s, []) for s in split_sentences(tail)]
    return [(t, ids) for t, ids in out if t.strip() or ids]


def verify(answer: str, ev: EvidenceSet, protected: list[str]) -> list[str]:
    """Problems in `answer`; an empty list means verified."""
    problems: list[str] = []
    names = sorted(
        {p for p in protected if p and any(c.isdigit() for c in p)}, key=len, reverse=True
    )
    for sent, ids in claims(answer):
        unknown = [i for i in ids if ev.get(i) is None]
        problems += [f"unknown citation {i}" for i in unknown]
        cited = ev.closure([i for i in ids if i not in unknown])
        text = CITE.sub(" ", sent)
        for n in names:
            text = re.sub(rf"(?<![\w-]){re.escape(n)}(?![\w-])", " ", text, flags=re.I)
        nums, dts = facts(cited)
        found, text = dates_in(text)
        sizes = [int(a) + int(b) / int(c) for a, b, c in FRACTION.findall(text)]
        text = FRACTION.sub(" ", text)
        tokens = NUMBER.findall(text)
        if (found or sizes or tokens) and not ids:
            problems.append(f"no citation for a sentence with numbers or dates: {sent[:80]!r}")
            continue
        for y, m, d in found:
            ok = (y, m, d) in dts if d is not None else any(x[:2] == (y, m) for x in dts)
            if not ok:
                problems.append(
                    f"date {y:04d}-{m:02d}{'' if d is None else f'-{d:02d}'} not in cited evidence"
                )
        for s in sizes:
            if not any(abs(s - v) < 1e-6 for v in nums):
                problems.append(f"size {s:g} in not in cited evidence")
        for tok in tokens:
            if matches(tok, nums):
                continue
            after = text[text.find(tok) + len(tok) :][:8].lower()
            if re.match(r"\s*(ft|feet|')", after) and any(
                abs(_num(tok) * FT - v) <= 0.5 * FT + 1e-6 for v in nums
            ):
                continue
            problems.append(f"number {tok} not in cited evidence")
    return problems
