"""DDR specifics: IADC-style activity codes and stitching multi-day events.

IADC codes follow the IADC daily drilling report time-distribution list (1 rig up/down,
2 drilling, 5 condition mud & circulate, 6 trips, 8 rig repair, 11 wireline logs, 12 run casing
& cement, 13 wait on cement, 14 nipple up BOP, 15 test BOP, 19 fishing, 21 other). Mapping is
from the operation text (keywords), because codes printed on real reports are inconsistent
across operators; a printed mnemonic, when present, is only used to check the mapping.
NPT categories (loss, stuck_pipe, kick, rig_repair) ride along with the code.
"""

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

# (pattern on operation text, IADC code, name, NPT category or None). First match wins.
ACTIVITY_RULES: list[tuple[str, int, str, str | None]] = [
    (
        r"lost circulation|losses|lost returns|loss(es)? of returns",
        21,
        "other: lost circulation",
        "loss",
    ),
    (r"pipe stuck|stuck pipe|stuck|jar(red|ring)|back.?off", 21, "other: stuck pipe", "stuck_pipe"),
    (
        r"\bkick\b|well control|shut.?in|pit gain|killed? (the )?well",
        21,
        "other: well control",
        "kick",
    ),
    (r"fish(ing)?\b", 19, "fishing", None),
    (r"rig repair|repair(ed)? (top drive|pump|drawworks)|breakdown", 8, "repair rig", "rig_repair"),
    (
        r"\brig(ging)? ?up|pre-?spud|\brig(ging)? ?down|rig released|tear ?down",
        1,
        "rig up / tear down",
        None,
    ),
    (r"\bdrill(ed|ing)\b(?!.*\bout\b)|\bmade hole", 2, "drilling", None),
    (r"ream(ed|ing)?|back.?ream", 3, "reaming", None),
    (r"circulat|condition(ed)? mud|bottoms up", 5, "condition mud & circulate", None),
    (r"\bpooh\b|\brih\b|trip(ped|ping)?\b|pull(ed)? out|run in hole", 6, "trips", None),
    (r"wire ?line|logging|\blogs?\b", 11, "wireline logs", None),
    (r"ran .*casing|run(ning)? casing|cement(ed|ing)", 12, "run casing & cement", None),
    (r"wait(ed|ing)? on cement|\bwoc\b", 13, "wait on cement", None),
    (r"nipple(d)? up|\bbop\b", 14, "nipple up BOP", None),
    (
        r"\blot\b|\bfit\b|leak.?off|formation integrity|drilled out shoe",
        21,
        "other: formation test",
        None,
    ),
]

# Printed mnemonic -> IADC code, used only to measure how well the text mapping agrees.
PRINTED_CODE_IADC = {
    "RIG": 1,
    "DRL": 2,
    "CIRC": 5,
    "TRIP": 6,
    "NPT-RR": 8,
    "LOG": 11,
    "CSG": 12,
    "CMT": 12,
    "WOC": 13,
    "BOP": 14,
    "TEST": 21,
    "NPT-LOSS": 21,
    "NPT-STUCK": 21,
    "NPT-KICK": 21,
}


def iadc(description: str) -> tuple[int, str, str | None]:
    t = description.lower()
    for pat, code, name, npt in ACTIVITY_RULES:
        if re.search(pat, t):
            return code, name, npt
    return 21, "other", None


@dataclass
class Row:
    report_date: date
    start: str  # HH:MM
    end: str
    hours: float
    description: str
    depth_m: float | None
    document_id: int | None = None
    page: int = 1
    bbox: list[float] = field(default_factory=lambda: [0.0, 0.0, 1.0, 1.0])
    confidence: float = 1.0

    @property
    def t0(self) -> datetime:
        return _at(self.report_date, self.start)

    @property
    def t1(self) -> datetime:
        return _at(self.report_date, self.end)


def _at(d: date, hhmm: str) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime.combine(d, datetime.min.time()) + timedelta(hours=h, minutes=m)


@dataclass
class StitchedEvent:
    type: str
    t_start: datetime
    t_end: datetime
    npt_hr: float
    depth_m: float | None
    timeline: list[dict[str, Any]]

    @property
    def days(self) -> int:
        return len({r["date"] for r in self.timeline})


def stitch(rows: list[Row]) -> list[StitchedEvent]:
    """Consecutive NPT rows of one category, contiguous in time (including across midnight into
    the next day's report), become one event with a timeline of its rows."""
    events: list[StitchedEvent] = []
    cur: StitchedEvent | None = None
    for r in sorted(rows, key=lambda r: r.t0):
        _, _, npt = iadc(r.description)
        if npt is None:
            cur = None
            continue
        entry = {
            "date": r.report_date.isoformat(),
            "from": r.start,
            "to": r.end,
            "hours": r.hours,
            "description": r.description,
            "document_id": r.document_id,
            "page": r.page,
            "bbox": r.bbox,
            "confidence": r.confidence,
        }
        if cur is not None and cur.type == npt and abs((r.t0 - cur.t_end).total_seconds()) < 60:
            cur.t_end = r.t1
            cur.npt_hr = round(cur.npt_hr + r.hours, 2)
            cur.timeline.append(entry)
        else:
            cur = StitchedEvent(npt, r.t0, r.t1, r.hours, r.depth_m, [entry])
            events.append(cur)
    return events


def depth_in(description: str, unit: str | None) -> float | None:
    """'Lost circulation at 9,689 ft in Sylhet' -> metres."""
    from pipelines.norm import depth_m

    m = re.search(r"\bat\s+([\d,]+(?:\.\d+)?\s*(?:m|ft)?)\b", description)
    if not m:
        return None
    try:
        return depth_m(m.group(1), unit)
    except ValueError:
        return None
