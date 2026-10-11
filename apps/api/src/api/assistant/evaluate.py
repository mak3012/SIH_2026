"""Assistant evaluation on the 100-question set (apps/api/tests/assistant_eval.jsonl).

Scores per question:
- answered questions: correct when every expected number appears in the answer (at the precision
  written), every expected date and text appears, and status is `answered`;
- not-in-records questions: status `not_in_records`; refusals: status `refused` by the expected
  rule;
- citations: hit when the answer cites one of the expected sources (document + page, or a voice
  note of the well); for aggregates, recall = share of the expected sources cited.

Verifier: reject rate = drafts that failed verification / all drafts. With the `noisy` drafter
(the rule composer, with one number changed in 30 % of first drafts) it also reports how many
corrupted drafts were caught and how many clean ones were wrongly rejected.
"""

import json
import random
import re
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from api.assistant.planner import compose
from api.assistant.service import Assistant, Draft, Result
from api.assistant.verify import NUMBER, dates_in, matches
from api.auth import User

EVAL_SET = Path(__file__).resolve().parents[3] / "tests" / "assistant_eval.jsonl"


def load_set(path: Path = EVAL_SET) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    return lines[0]["meta"], lines[1:]


def _numbers_in(answer: str) -> list[str]:
    text = re.sub(r"\[E\d+\]", " ", answer)
    text = re.sub(r"\b[A-Z]{1,6}-\d{1,4}\b", " ", text)  # well names
    return NUMBER.findall(dates_in(text)[1])


def score(q: dict[str, Any], r: Result) -> dict[str, Any]:
    exp = q["expect"]
    out: dict[str, Any] = {"id": q["id"], "category": q["category"], "status": r.status}
    if exp["status"] != "answered":
        ok = r.status == exp["status"] and (exp.get("rule") is None or r.refusal == exp["rule"])
        return {**out, "correct": ok, "citation": None}
    toks = _numbers_in(r.answer)
    found_dates = {f"{y:04d}-{m:02d}-{d:02d}" for y, m, d in dates_in(r.answer)[0] if d}
    ok = r.status == "answered"
    for v in exp.get("numbers", []):
        if isinstance(v, str):
            ok &= v in found_dates
        else:
            ok &= any(matches(t, [float(v)]) for t in toks)
    low = r.answer.lower()
    ok &= all(t.lower() in low for t in exp.get("text", []))
    want = exp.get("citations") or []
    cit = None
    if want:
        got = r.citations

        def hit(w: dict[str, Any]) -> bool:
            if w.get("type") == "audio":
                return any(c.get("type") == "audio" and w["well"] in str(c.get("uri")) for c in got)
            return any(
                c.get("document") == w["document"] and c.get("page") == w["page"] for c in got
            )

        hits = [hit(w) for w in want]
        cit = sum(hits) / len(hits) if exp.get("citation_mode") == "recall" else float(any(hits))
    return {**out, "correct": bool(ok), "citation": cit}


def noisy_drafter(
    p: float = 0.3, seed: int = 0
) -> tuple[Callable[[Draft, str, int], dict[str, Any] | None], list[bool]]:
    """A drafter that writes the rule answer but, in a share p of first drafts, changes one
    number (as a careless model would). Returns it and a log: one entry per draft, True when
    that draft was corrupted."""
    rng = random.Random(seed)
    log: list[bool] = []

    def draft(d: Draft, feedback: str, attempt: int) -> dict[str, Any] | None:
        text = compose(d.question, d.intent, d.entities, d.evidence, d.records, d.passages)
        if not text:
            log.append(False)
            return {"answer": "", "citations": [], "not_in_records": True}
        masked = re.sub(r"\[E\d+\]|\b[A-Z]{1,6}-\d{1,4}\b", lambda x: "#" * len(x.group(0)), text)
        nums = list(NUMBER.finditer(masked))
        bad = attempt == 0 and bool(nums) and rng.random() < p
        if bad:
            m = rng.choice(nums)
            v = float(m.group(0).replace(",", ""))
            text = text[: m.start()] + f"{v * rng.choice([1.07, 0.93]) + 3:,.1f}" + text[m.end() :]
        log.append(bad)
        return {"answer": text, "citations": [], "not_in_records": False}

    return draft, log


def run(
    assistant: Assistant,
    questions: list[dict[str, Any]],
    user: User,
    noisy_log: list[bool] | None = None,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    by_id: dict[str, Result] = {}
    drafts = rejected = 0
    confusion = {"corrupted": 0, "caught": 0, "missed": 0, "clean": 0, "false_rejects": 0}
    for q in questions:
        start = len(noisy_log) if noisy_log is not None else 0
        prev = by_id.get(q.get("follow_up_of", ""))
        r = assistant.ask(
            user,
            q["question"],
            prev.entities if prev else None,
            [(questions_by(questions, q["follow_up_of"]), prev.answer)] if prev else None,
        )
        by_id[q["id"]] = r
        attempts = (r.verification or {}).get("attempts", [])
        drafts += len(attempts)
        rejected += sum(1 for a in attempts if a)
        if noisy_log is not None:
            for bad, problems in zip(noisy_log[start:], attempts, strict=False):
                key = (
                    ("caught" if problems else "missed")
                    if bad
                    else ("false_rejects" if problems else None)
                )
                confusion["corrupted" if bad else "clean"] += 1
                if key:
                    confusion[key] += 1
        keys = (
            "id",
            "type",
            "document",
            "page",
            "bbox",
            "uri",
            "t_start",
            "t_end",
            "view",
            "derived_from",
        )
        compact = [
            {k: c[k] for k in keys if c.get(k) is not None} for c in r.citations if c.get("direct")
        ]
        results.append(
            {
                **score(q, r),
                "question": q["question"],
                "answer": r.answer,
                "attempts": attempts,
                "citations": compact,
                "followups": r.followups,
            }
        )
    cats: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for x in results:
        cats[x["category"]].append(x)
    cites = [x["citation"] for x in results if x["citation"] is not None]
    return {
        "questions": len(results),
        "accuracy": sum(bool(x["correct"]) for x in results) / len(results),
        "citation": sum(cites) / len(cites) if cites else None,
        "drafts": drafts,
        "verifier_rejects": rejected,
        "verifier_reject_rate": rejected / drafts if drafts else 0.0,
        "noisy": confusion if noisy_log is not None else None,
        "unverified_final": sum(
            1 for x in results if x["status"] == "not_in_records" and any(x["attempts"])
        ),
        "by_category": {
            c: {
                "n": len(v),
                "accuracy": sum(x["correct"] for x in v) / len(v),
                "citation": (
                    sum(x["citation"] for x in v if x["citation"] is not None)
                    / max(1, sum(x["citation"] is not None for x in v))
                ),
            }
            for c, v in sorted(cats.items())
        },
        "results": results,
    }


def questions_by(questions: list[dict[str, Any]], qid: str) -> str:
    return str(next(q["question"] for q in questions if q["id"] == qid))


def report(reports: dict[str, dict[str, Any]]) -> str:
    modes = list(reports)
    rv = list(reports.values())
    lines = [
        "| | " + " | ".join(modes) + " |",
        "|---|" + "---|" * len(modes),
        "| Answer accuracy | " + " | ".join(f"{r['accuracy']:.0%}" for r in rv) + " |",
        "| Citation hit / recall | " + " | ".join(f"{r['citation']:.0%}" for r in rv) + " |",
        "| Drafts | " + " | ".join(str(r["drafts"]) for r in rv) + " |",
        "| Verifier reject rate | "
        + " | ".join(f"{r['verifier_reject_rate']:.1%}" for r in rv)
        + " |",
        '| Final "not in the records" after failed verification | '
        + " | ".join(str(r["unverified_final"]) for r in rv)
        + " |",
    ]
    cats = sorted({c for r in rv for c in r["by_category"]})
    lines += [
        "",
        "| Category | n | " + " | ".join(f"accuracy ({m})" for m in modes) + " |",
        "|---|---|" + "---|" * len(modes),
    ]
    for c in cats:
        lines.append(
            f"| {c} | {rv[0]['by_category'][c]['n']} | "
            + " | ".join(f"{r['by_category'][c]['accuracy']:.0%}" for r in rv)
            + " |"
        )
    for m, r in reports.items():
        if r.get("noisy"):
            n = r["noisy"]
            lines += [
                "",
                f"`{m}`: {n['corrupted']} drafts had one number changed; the verifier "
                f"rejected {n['caught']} of them ({n['missed']} missed) and "
                f"{n['false_rejects']} of {n['clean']} clean drafts.",
            ]
    return "\n".join(lines)
