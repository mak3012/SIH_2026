"""Structured evidence: rule templates that turn (intent, entities) into parameterized queries
over the chat.v_* views, and a deterministic composer that writes an answer from the rows.

The composer is the drafter when no LLM is configured, and the reference for what a correct
answer looks like: every number it writes comes from a cited row, formatted at a precision the
verifier accepts.
"""

import re
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from api.assistant.entities import Entities
from api.assistant.evidence import Evidence, EvidenceSet

EVENT_WORDS = {
    "loss": "loss",
    "kick": "kick",
    "stuck_pipe": "stuck pipe",
    "tight_hole": "tight hole",
    "pack_off": "pack-off",
    "washout": "washout",
    "cement_issue": "cement problem",
    "fishing": "fishing",
    "torque_spike": "torque spike",
    "other": "problem",
}
EVENT_ATTRS = {"mw", "ecd", "npt", "volume", "date", "cause", "mitigation", "severity"}
MAX_LIST = 6


@dataclass
class Query:
    purpose: str  # tops | casing | events | wells
    sql: str
    params: dict[str, Any]
    rows: list[dict[str, Any]] = field(default_factory=list)


def plan(intent: str, e: Entities) -> list[Query]:
    a = set(e.attributes)
    qs: list[Query] = []
    where, params = [], {}
    if e.wells:
        where.append("well = ANY(%(wells)s)")
        params["wells"] = e.wells

    def q(purpose: str, view: str, extra: list[str], p: dict[str, Any], order: str) -> Query:
        w = where + extra
        sql = f"SELECT * FROM chat.{view}" + (f" WHERE {' AND '.join(w)}" if w else "")
        return Query(purpose, f"{sql} ORDER BY {order}", {**params, **p})

    extra: list[str] = []
    p: dict[str, Any] = {}
    if a & {"shoe", "shoe_test", "casing"} or (e.casing_in and not e.hazards):
        if e.casing_in:
            extra.append("od_in = ANY(%(sizes)s)")
            p["sizes"] = e.casing_in
        if "shoe_test" in a:
            extra.append("shoe_test IS NOT NULL")
        qs.append(q("casing", "v_casing", extra, p, "well, md_shoe"))
    elif e.hazards or a & EVENT_ATTRS or (intent == "explain" and (e.wells or e.formations)):
        extra, p = [], {}
        if e.formations:
            extra.append("formation = ANY(%(formations)s)")
            p["formations"] = e.formations
        if e.hazards:
            extra.append("event_type = ANY(%(hazards)s)")
            p["hazards"] = e.hazards
        if e.depths_m:
            extra.append("abs(md_top - %(depth)s) <= greatest(25, 0.02 * %(depth)s)")
            p["depth"] = e.depths_m[0]
        qs.append(q("events", "v_events", extra, p, "well, md_top"))
    elif e.formations or "top" in a:
        extra, p = [], {}
        if e.formations:
            extra.append("formation = ANY(%(formations)s)")
            p["formations"] = e.formations
        qs.append(q("tops", "v_tops", extra, p, "well, md"))
    elif e.wells and a & {"spud", "td", "location", "field", "depth"}:
        qs.append(q("wells", "v_wells", [], {}, "well"))
    return qs


# ------------------------------------------------------------------ formatting


def m(v: float | None) -> str:
    return "?" if v is None else f"{v:,.1f}"


def sg(v: float | None) -> str:
    return "?" if v is None else f"{v:.2f}"


def hrs(v: float | None) -> str:
    return "?" if v is None else f"{v:g}"


def size(v: float) -> str:
    whole, frac = int(v), v - int(v)
    eighths = round(frac * 8)
    if eighths == 0:
        return f"{whole}"
    n, d = eighths, 8
    while n % 2 == 0:
        n, d = n // 2, d // 2
    return f"{whole}-{n}/{d}"


def day(v: Any) -> str:
    return (
        v.date().isoformat()
        if isinstance(v, datetime)
        else (v.isoformat() if isinstance(v, date) else str(v))
    )


# ------------------------------------------------------------------ rows -> evidence


def row_text(purpose: str, r: dict[str, Any]) -> str:
    if purpose == "tops":
        return f"{r['well']}: {r['formation']} top at {m(r['md'])} m MD, {m(r['tvdss'])} m TVDSS"
    if purpose == "casing":
        t = (
            f"{r['well']}: {size(r['od_in'])} in {r['kind']} casing, shoe at {m(r['md_shoe'])} m MD"
            f", {r['grade']}, {r['weight_ppf']:g} lb/ft"
        )
        if r.get("shoe_test"):
            t += f"; {r['shoe_test'].upper()} below the shoe {sg(r['shoe_test_emw'])} sg EMW"
        return t
    if purpose == "events":
        t = (
            f"{r['well']}: {EVENT_WORDS.get(r['event_type'], r['event_type'])} at "
            f"{m(r['md_top'])} m MD in {r['formation']}"
        )
        if r.get("t_start"):
            t += f" on {day(r['t_start'])}"
        return t + f". {r.get('narrative') or ''}".rstrip()
    if purpose == "wells":
        return (
            f"{r['well']}: field {r['field']}, block {r['block_id']}, spud {day(r['spud_date'])}, "
            f"TD {m(r['td_md'])} m MD"
        )
    return str(r)


def to_evidence(ev: EvidenceSet, queries: list[Query]) -> dict[str, list[Evidence]]:
    out: dict[str, list[Evidence]] = {}
    for qy in queries:
        for r in qy.rows:
            keep = {k: v for k, v in r.items() if k not in ("surface", "position", "synonyms")}
            out.setdefault(qy.purpose, []).append(
                ev.add(
                    "record",
                    row_text(qy.purpose, r),
                    values=keep,
                    source_ref_id=r.get("source_ref_id"),
                    query={
                        "view": re.search(r"chat\.(v_\w+)", qy.sql)[1],  # type: ignore[index]
                        "sql": qy.sql,
                        "params": qy.params,
                    },
                )
            )
    return out


# ------------------------------------------------------------------ composer


OPS = [
    (
        "count",
        r"\bhow many\b(?!\s+(hours?|barrels?|bbl|days?|metres?|meters?|feet|ft|m|tons?))"
        r"|\bnumber of\b|\bcount\b",
    ),
    ("sum", r"\btotal\b|\bin all\b|\bsum\b|\baltogether\b"),
    ("avg", r"\baverage\b|\bmean\b|\btypical\b"),
    ("max", r"\b(highest|deepest|largest|most|maximum|max|heaviest|longest|biggest|worst)\b"),
    ("min", r"\b(lowest|shallowest|smallest|least|minimum|min|lightest|shortest)\b"),
]


def op_of(q: str) -> str:
    return next((op for op, pat in OPS if re.search(pat, q, re.I)), "list")


def field_of(purpose: str, attrs: set[str], q: str) -> tuple[str, str, Any]:
    """(column, label, formatter) the question is about."""
    if purpose == "events":
        for a, col, label, fmt in (
            ("npt", "npt_hr", "NPT", lambda v: f"{hrs(v)} h"),
            ("mw", "mw", "mud weight", lambda v: f"{sg(v)} sg"),
            ("ecd", "ecd", "ECD", lambda v: f"{sg(v)} sg"),
            ("volume", "loss_bbl", "volume lost", lambda v: f"{v:g} bbl"),
            ("severity", "severity", "severity", lambda v: f"{v}"),
        ):
            if a in attrs:
                return col, label, fmt
        return "md_top", "depth", lambda v: f"{m(v)} m MD"
    if purpose == "casing":
        if "shoe_test" in attrs:
            return "shoe_test_emw", "shoe-test EMW", lambda v: f"{sg(v)} sg"
        return "md_shoe", "shoe depth", lambda v: f"{m(v)} m MD"
    if purpose == "tops":
        if re.search(r"\btvdss\b|\bsub[\s-]?sea\b", q, re.I):
            return "tvdss", "top", lambda v: f"{m(v)} m TVDSS"
        return "md", "top", lambda v: f"{m(v)} m MD"
    return "td_md", "total depth", lambda v: f"{m(v)} m MD"


def _subject(purpose: str, e: Entities) -> str:
    if purpose == "events":
        kinds = ", ".join(EVENT_WORDS.get(h, h) for h in e.hazards) or "drilling problem"
        where = f" in the {', '.join(e.formations)}" if e.formations else ""
        return f"{kinds} events{where}"
    if purpose == "tops":
        return f"{', '.join(e.formations) or 'formation'} tops"
    if purpose == "casing":
        sizes = ", ".join(f"{size(s)} in" for s in e.casing_in)
        return f"{sizes} casing strings".strip()
    return "wells"


def compose(
    question: str,
    intent: str,
    e: Entities,
    ev: EvidenceSet,
    records: dict[str, list[Evidence]],
    passages: list[Evidence],
) -> str:
    """A plain answer whose every number is a cited value. Empty when nothing fits."""
    attrs = set(e.attributes)
    parts: list[str] = []
    for purpose, rows in records.items():
        if not rows:
            continue
        col, label, fmt = field_of(purpose, attrs, question)
        if intent == "aggregate":
            parts.append(_aggregate(question, purpose, e, ev, rows, col, label, fmt))
        elif intent == "explain" and purpose == "events":
            parts += [_explain_event(r) for r in rows[:3]]
        else:
            parts += [_lookup(purpose, attrs, r, col, fmt, question) for r in rows[:MAX_LIST]]
            if len(rows) > MAX_LIST:
                parts.append("More records match; ask for a total or a list.")
    if intent == "explain" or not parts:
        for p in passages[: 2 if parts else 3]:
            parts.append(f'{_source_label(p)}: "{_passage(p.text)}" [{p.id}].')
    return " ".join(x for x in parts if x)


def _source_label(p: Evidence) -> str:
    c = p.citation or {}
    if c.get("type") == "audio":
        return "A voice note says"
    if c.get("document"):
        return f"{c['document']}, page {c.get('page')}, says"
    return "A record says"


def _passage(text: str) -> str:
    return text.split(": ", 1)[1] if ": " in text[:60] else text


def _lookup(purpose: str, attrs: set[str], r: Evidence, col: str, fmt: Any, q: str) -> str:
    v = r.values
    cite = f"[{r.id}]"
    if purpose == "tops":
        return (
            f"In {v['well']} the {v['formation']} top is at {m(v['md'])} m MD "
            f"({m(v['tvdss'])} m TVDSS) {cite}."
        )
    if purpose == "casing":
        what = f"the {size(v['od_in'])} in {v['kind']} casing"
        if "shoe_test" in attrs and v.get("shoe_test"):
            return (
                f"In {v['well']} the {v['shoe_test'].upper()} below the shoe of {what} "
                f"({m(v['md_shoe'])} m MD) gave {sg(v['shoe_test_emw'])} sg EMW {cite}."
            )
        s = f"In {v['well']} the shoe of {what} is at {m(v['md_shoe'])} m MD"
        if "casing" in attrs:
            hole = size(v["hole_size_in"])
            s += f", grade {v['grade']}, {v['weight_ppf']:g} lb/ft, in {hole} in hole"
        return s + f" {cite}."
    if purpose == "wells":
        if "spud" in attrs or "date" in attrs:
            return f"{v['well']} was spudded on {day(v['spud_date'])} {cite}."
        if "field" in attrs:
            return f"{v['well']} is in the {v['field']} field, block {v['block_id']} {cite}."
        if "location" in attrs:
            where = f"latitude {v['lat']:.4f}, longitude {v['lon']:.4f} (WGS 84)"
            return f"{v['well']} is at {where} {cite}."
        return f"{v['well']} reached a total depth of {m(v['td_md'])} m MD {cite}."
    # events
    kind = EVENT_WORDS.get(v["event_type"], v["event_type"])
    head = f"In {v['well']} the {kind} at {m(v['md_top'])} m MD in the {v['formation']}"
    if "date" in attrs and v.get("t_start"):
        return f"{head} happened on {day(v['t_start'])} {cite}."
    if "cause" in attrs:
        cause = v.get("root_cause")
        return (
            f"{head}: cause ({v['root_cause_status']}) {cause} {cite}."
            if cause
            else f"{head}: no root cause is recorded {cite}."
        )
    if "mitigation" in attrs:
        actions = v.get("mitigations") or "none recorded"
        return f"{head}: actions taken: {actions}; outcome: {v.get('outcome')} {cite}."
    if col == "loss_bbl" and v.get("loss_bbl") is None:
        return f"{head}: {v.get('narrative')} {cite}."
    if col != "md_top" and v.get(col) is not None:
        return f"{head}: {fmt(v[col])} {cite}."
    if col != "md_top":
        return f"{head}: the {col.replace('_', ' ')} is not recorded {cite}."
    when = f" on {day(v['t_start'])}" if v.get("t_start") else ""
    return f"{head}{when} {cite}."


def _explain_event(r: Evidence) -> str:
    v = r.values
    kind = EVENT_WORDS.get(v["event_type"], v["event_type"])
    s = f"In {v['well']} the {kind} at {m(v['md_top'])} m MD in the {v['formation']}"
    if v.get("t_start"):
        s += f" ({day(v['t_start'])})"
    narrative = (v.get("narrative") or "no narrative recorded").strip().rstrip(".")
    s += f": {narrative} [{r.id}]."
    low = narrative.lower()
    cause = v.get("root_cause")
    if cause and cause.lower() not in low:
        s += f" Cause ({v['root_cause_status']}): {cause} [{r.id}]."
    elif not cause:
        s += " No root cause is recorded."
    if v.get("mitigations") and v["mitigations"].split(";")[0].strip().lower() not in low:
        s += f" Actions: {v['mitigations']}; outcome: {v.get('outcome')} [{r.id}]."
    return s


def _aggregate(
    q: str,
    purpose: str,
    e: Entities,
    ev: EvidenceSet,
    rows: list[Evidence],
    col: str,
    label: str,
    fmt: Any,
) -> str:
    op = op_of(q)
    subject = _subject(purpose, e)
    ids = [r.id for r in rows]
    wells = sorted({str(r.values.get("well")) for r in rows})
    if re.search(r"\bwhich wells?\b.*\bmost\b|\bmost\b.*\bwhich wells?\b", q, re.I) or (
        op == "max" and re.search(r"\bmost (losses|kicks|events|problems)\b", q, re.I)
    ):
        per: dict[str, list[str]] = {}
        for r in rows:
            per.setdefault(str(r.values["well"]), []).append(r.id)
        top = max(per.values(), key=len)
        best = [w for w, v in per.items() if len(v) == len(top)]
        agg = ev.add(
            "aggregate",
            f"{subject} per well, most: {', '.join(best)} with {len(top)}",
            values={"count": len(top)},
            derived_from=[i for w in best for i in per[w]],
        )
        return f"{' and '.join(best)} had the most {subject}: {len(top)} each [{agg.id}]."
    if op == "count" or (op == "list" and re.search(r"\bwhich wells?\b|\blist\b", q, re.I)):
        n_wells = len(wells)
        agg = ev.add(
            "aggregate",
            f"{len(rows)} {subject} in {n_wells} wells",
            values={"count": len(rows), "wells": n_wells},
            derived_from=ids,
        )
        listed = ", ".join(wells[:12]) + (" and others" if len(wells) > 12 else "")
        return f"The records show {len(rows)} {subject} in {n_wells} wells [{agg.id}]: {listed}."
    vals: list[tuple[Evidence, Any]] = [
        (r, r.values[col]) for r in rows if r.values.get(col) is not None
    ]
    if not vals:
        return f"No {label} is recorded for the {subject} found [{ids[0]}]."
    nums = [float(v) for _, v in vals]
    if op in ("sum", "avg"):
        val = sum(nums) if op == "sum" else statistics.fmean(nums)
        word = "in total" if op == "sum" else "on average"
        agg = ev.add(
            "aggregate",
            f"{op} of {label} over {len(nums)} {subject}",
            values={op: round(val, 4), "n": len(nums)},
            derived_from=[r.id for r, _ in vals],
        )
        return f"The {label} of the {len(nums)} {subject} is {fmt(val)} {word} [{agg.id}]."
    if op in ("max", "min"):
        r, v = (max if op == "max" else min)(vals, key=lambda x: float(x[1]))
        word = "highest" if op == "max" else "lowest"
        if col in ("md_top", "md", "md_shoe", "tvdss"):
            word = "deepest" if op == "max" else "shallowest"
        if purpose == "tops":
            return (
                f"The {word} {r.values['formation']} top is at {fmt(v)}, in "
                f"{r.values['well']} [{r.id}]."
            )
        return (
            f"The {word} {label} among the {subject} is {fmt(v)}, in {r.values['well']} [{r.id}]."
        )
    listed = "; ".join(f"{r.values['well']} {fmt(v)} [{r.id}]" for r, v in vals[:MAX_LIST])
    more = "; and more" if len(vals) > MAX_LIST else ""
    return f"{label.capitalize()} for the {subject}: {listed}{more}."
