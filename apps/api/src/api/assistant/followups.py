"""Suggested follow-up questions: built by rules from what the answer was about, so each one is
answerable from the same records (they name wells, formations and hazards that exist)."""

from api.assistant.entities import Entities
from api.assistant.evidence import Evidence
from api.assistant.planner import EVENT_WORDS, size

MAX = 3


def suggest(kind: str, e: Entities, records: dict[str, list[Evidence]]) -> list[str]:
    out: list[str] = []
    rows = [r.values for rs in records.values() for r in rs]
    wells = e.wells or sorted({str(v["well"]) for v in rows if v.get("well")})[:1]
    well = wells[0] if wells else None
    forms = e.formations or sorted({str(v["formation"]) for v in rows if v.get("formation")})[:1]
    form = forms[0] if forms else None
    hazards = e.hazards or sorted({str(v["event_type"]) for v in rows if v.get("event_type")})[:1]
    hz = EVENT_WORDS.get(hazards[0], hazards[0]) if hazards else None

    if kind == "refused":
        return [
            "Which wells had kicks, and how were they handled?",
            "What mud weights did offset wells use where they had losses?",
        ]
    if "events" in records:
        if well and hz:
            out.append(f"What was done about the {hz} in {well}, and did it work?")
        if form and hz:
            out.append(f"How many {hz} events were recorded in the {form} across all wells?")
        if well:
            out.append(f"What casing was set in {well}, and what did the shoe tests give?")
    if "tops" in records and form:
        out.append(f"What drilling problems were recorded in the {form}?")
        if well:
            out.append(f"What casing was set in {well}?")
    if "casing" in records and well:
        sizes = sorted({float(v["od_in"]) for v in rows if v.get("od_in")})
        out.append(f"What problems happened below the shoe in {well}?")
        if sizes:
            out.append(f"Which wells had a LOT or FIT below the {size(sizes[0])} in shoe?")
    if kind == "not_in_records":
        if well:
            out.append(f"What drilling problems were recorded in {well}?")
        out.append("Which wells had losses in the Barail?")
    return list(dict.fromkeys(out))[:MAX]
