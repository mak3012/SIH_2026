"""Rule validation of extracted values (canonical units). Rules decide; nothing here guesses.

Each rule returns failures naming the fields it implicates. A field's rule score is 1.0 when no
rule fails for it, 0.5 when only soft rules fail, 0.0 when a hard rule fails. Add a rule by
writing a function `(values, ctx) -> list[Failure]` and listing it in RULES with its id.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from core.canonical.equipment import CASING_OD, GRADES, HOLE_SIZES  # noqa: E402
from pipelines.extract import Value
from pipelines.norm import DEPTH_FIELDS, leaf, size_inches

MAX_DEPTH_M = 8000.0


@dataclass
class Failure:
    rule: str
    severity: str  # 'hard' | 'soft'
    fields: list[str]
    message: str


Values = dict[str, Value]
Rule = Callable[[Values, dict[str, Any]], list[Failure]]


def _v(values: Values, name: str) -> Any:
    x = values.get(name)
    return None if x is None else x.value


def _indexed(values: Values, prefix: str, key: str) -> list[tuple[str, Any]]:
    pat = re.compile(rf"^{re.escape(prefix)}\[(\d+)\]\.{key}$")
    hits = [(int(m.group(1)), n) for n in values if (m := pat.match(n))]
    return [(n, values[n].value) for _, n in sorted(hits)]


def parse_errors(values: Values, ctx: dict[str, Any]) -> list[Failure]:
    return [
        Failure("R-PARSE", "hard", [n], f"unreadable: {v.error}")
        for n, v in values.items()
        if v.error
    ]


def ranges(values: Values, ctx: dict[str, Any]) -> list[Failure]:
    out = []
    for n, x in values.items():
        v, k = x.value, leaf(n)
        if not isinstance(v, int | float):
            continue
        lo, hi = {
            "north": (-MAX_DEPTH_M, MAX_DEPTH_M),
            "east": (-MAX_DEPTH_M, MAX_DEPTH_M),
            "datum_elevation": (-500.0, 6000.0),
            "ground_elevation": (-500.0, 6000.0),
            "tvdss": (-6000.0, MAX_DEPTH_M),
            "mw": (0.8, 2.6),
            "emw": (0.8, 2.8),
            "mud_weight": (0.8, 2.6),
            "inc": (0.0, 180.0),
            "azi": (0.0, 360.0),
            "hours": (0.0, 24.0),
            "total_hours": (0.0, 24.0),
            "npt_hours": (0.0, 24.0),
            "npt_hr": (0.0, 1000.0),
            "volume_bbl": (0.0, 20000.0),
            "loss_bbl": (0.0, 20000.0),
            "pit_gain_bbl": (0.0, 1000.0),
            "weight_ppf": (5.0, 200.0),
            "latitude": (-90.0, 90.0),
            "longitude": (-180.0, 180.0),
        }.get(k, (0.0, MAX_DEPTH_M) if k in DEPTH_FIELDS else (float("-inf"), float("inf")))
        if not lo <= v <= hi:
            out.append(Failure("R-RANGE", "hard", [n], f"{v} outside [{lo}, {hi}]"))
    return out


def monotonic(values: Values, ctx: dict[str, Any]) -> list[Failure]:
    out = []
    for prefix, key, strict in (
        ("tops", "md", True),
        ("survey", "md", True),
        ("survey", "tvd", False),
        ("casing", "shoe_md", True),
    ):
        seq = [(n, v) for n, v in _indexed(values, prefix, key) if isinstance(v, int | float)]
        for (na, a), (nb, b) in zip(seq, seq[1:], strict=False):
            if b < a or (strict and b == a):
                out.append(
                    Failure(
                        "R-MONOTONIC",
                        "hard",
                        [na, nb],
                        f"{prefix} {key} not increasing: {a} then {b}",
                    )
                )
    for prefix in ("tops", "survey"):
        for n_md, md in _indexed(values, prefix, "md"):
            n_tvd = n_md[: -len("md")] + "tvd"
            tvd = _v(values, n_tvd)
            if isinstance(md, int | float) and isinstance(tvd, int | float) and tvd > md + 0.5:
                out.append(Failure("R-TVD-LE-MD", "hard", [n_md, n_tvd], f"TVD {tvd} > MD {md}"))
    return out


def casing_sizes(values: Values, ctx: dict[str, Any]) -> list[Failure]:
    out = []
    for n, v in _indexed(values, "casing", "od_in") + _indexed(values, "lot_fit", "casing"):
        if v is not None and v not in CASING_OD:
            out.append(Failure("R-CASING-STD", "hard", [n], f'{v}" is not a standard casing OD'))
    holes = _indexed(values, "casing", "hole_in") + [
        (n, x.value) for n, x in values.items() if n == "hole_size"
    ]
    for n, v in holes:
        if v is not None and v not in HOLE_SIZES:
            out.append(Failure("R-HOLE-STD", "soft", [n], f'{v}" is not a common bit size'))
    for n, v in _indexed(values, "casing", "grade"):
        if v is not None and str(v).upper() not in GRADES:
            out.append(Failure("R-GRADE", "soft", [n], f"unknown casing grade {v}"))
    ods = [(n, v) for n, v in _indexed(values, "casing", "od_in") if v in CASING_OD]
    for (na, a), (nb, b) in zip(ods, ods[1:], strict=False):
        if size_inches(b) >= size_inches(a):
            out.append(
                Failure("R-CASING-ORDER", "hard", [na, nb], f'{b}" inside {a}" must be smaller')
            )
    for i, (n, od) in enumerate(_indexed(values, "casing", "od_in")):
        hole = _v(values, f"casing[{i}].hole_in")
        if od in CASING_OD and hole in HOLE_SIZES and size_inches(hole) <= size_inches(od):
            out.append(
                Failure(
                    "R-HOLE-GT-OD",
                    "hard",
                    [n, f"casing[{i}].hole_in"],
                    f'hole {hole}" not larger than casing {od}"',
                )
            )
    return out


def ddr_hours(values: Values, ctx: dict[str, Any]) -> list[Failure]:
    rows = _indexed(values, "time_breakdown", "hours")
    if not rows:
        return []
    out = []
    total = sum(v for _, v in rows if isinstance(v, int | float))
    if abs(total - 24) > 0.01:
        out.append(
            Failure(
                "R-DDR-24H",
                "hard",
                [n for n, _ in rows],
                f"time breakdown sums to {total:g} h, not 24",
            )
        )
    printed_total = _v(values, "total_hours")
    if isinstance(printed_total, int | float) and abs(printed_total - 24) > 0.01:
        out.append(Failure("R-DDR-24H", "hard", ["total_hours"], f"total {printed_total:g} h"))
    prev_to = None
    for i, (n, hours) in enumerate(rows):
        f, t = _v(values, f"time_breakdown[{i}].from"), _v(values, f"time_breakdown[{i}].to")
        if isinstance(f, str) and isinstance(t, str) and isinstance(hours, int | float):
            span = _minutes(t) - _minutes(f)
            if abs(span / 60 - hours) > 0.01:
                out.append(Failure("R-DDR-ROW", "hard", [n], f"{f}-{t} is not {hours:g} h"))
            if prev_to is not None and f != prev_to:
                out.append(
                    Failure(
                        "R-DDR-GAP",
                        "hard",
                        [f"time_breakdown[{i}].from"],
                        f"row starts {f}, previous ended {prev_to}",
                    )
                )
            prev_to = t
    npt = _v(values, "npt_hours")
    if isinstance(npt, int | float):
        coded = sum(
            v
            for i, (_, v) in enumerate(rows)
            if isinstance(v, int | float)
            and str(_v(values, f"time_breakdown[{i}].code") or "").upper().startswith("NPT")
        )
        if abs(coded - npt) > 0.01:
            out.append(
                Failure(
                    "R-DDR-NPT",
                    "soft",
                    ["npt_hours"],
                    f"NPT {npt:g} h but NPT rows sum to {coded:g} h",
                )
            )
    return out


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def depth_le_td(values: Values, ctx: dict[str, Any]) -> list[Failure]:
    td = _v(values, "td_md") or ctx.get("td_md")
    if not isinstance(td, int | float):
        return []
    out = []
    for n, x in values.items():
        k = leaf(n)
        if k in ("md", "shoe_md", "depth_md") and isinstance(x.value, int | float):
            if x.value > td + 1.0:
                out.append(Failure("R-DEPTH-LE-TD", "hard", [n], f"{x.value} m below TD {td} m"))
    return out


def dates(values: Values, ctx: dict[str, Any]) -> list[Failure]:
    spud, done = _v(values, "spud_date"), _v(values, "completion_date")
    out = []
    if isinstance(spud, str) and isinstance(done, str) and spud > done:
        out.append(
            Failure(
                "R-DATES",
                "hard",
                ["spud_date", "completion_date"],
                f"spud {spud} after completion {done}",
            )
        )
    if isinstance(spud, str) and isinstance(done, str):
        for n, d in _indexed(values, "events", "date"):
            if isinstance(d, str) and not spud <= d <= done:
                out.append(Failure("R-DATES", "soft", [n], f"event {d} outside {spud}..{done}"))
    for n, x in values.items():
        if (
            leaf(n) in ("date", "report_date", "spud_date", "completion_date")
            and isinstance(x.value, str)
            and not date(1900, 1, 1).isoformat() <= x.value <= date.today().isoformat()
        ):
            out.append(Failure("R-DATES", "hard", [n], f"implausible date {x.value}"))
    return out


def required(values: Values, ctx: dict[str, Any]) -> list[Failure]:
    need = {
        "wcr": ["well_name", "td_md"],
        "ddr": ["well_name", "report_date"],
        "survey_report": ["well_name"],
        "field_note": ["well_name", "event_type"],
    }
    return [
        Failure("R-REQUIRED", "hard", [n], f"{n} missing")
        for n in need.get(ctx.get("doc_type", ""), [])
        if _v(values, n) in (None, "")
    ]


def coordinates(values: Values, ctx: dict[str, Any]) -> list[Failure]:
    if "latitude" in values and _v(values, "coordinate_datum") is None:
        return [
            Failure(
                "R-DATUM", "hard", ["latitude", "longitude"], "coordinates without a stated datum"
            )
        ]
    return []


RULES: dict[str, Rule] = {
    "parse": parse_errors,
    "range": ranges,
    "monotonic": monotonic,
    "casing": casing_sizes,
    "ddr_hours": ddr_hours,
    "depth_le_td": depth_le_td,
    "dates": dates,
    "required": required,
    "coordinates": coordinates,
}


def validate(values: Values, ctx: dict[str, Any]) -> tuple[dict[str, float], list[Failure]]:
    """Rule score per field and every failure."""
    failures = [f for rule in RULES.values() for f in rule(values, ctx)]
    score = dict.fromkeys(values, 1.0)
    for f in failures:
        for n in f.fields:
            if n in score:
                score[n] = min(score[n], 0.0 if f.severity == "hard" else 0.5)
    return score, failures
