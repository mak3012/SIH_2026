"""Document type from its first pages' text, file name and registered type (weighted votes)."""

import re

TYPES = ("wcr", "ddr", "survey_report", "field_note")
KEYWORDS: dict[str, list[tuple[str, float]]] = {
    "wcr": [
        ("well completion report", 5),
        ("completion report", 3),
        ("formation tops", 2),
        ("casing and cementing", 2),
        ("rig release", 1),
        ("spud date", 1),
    ],
    "ddr": [
        ("daily drilling report", 5),
        ("time breakdown", 3),
        ("depth @ 24:00", 2),
        ("report no", 1),
        ("npt today", 1),
    ],
    "survey_report": [
        ("directional survey", 5),
        ("survey report", 3),
        ("inc", 0.5),
        ("azi", 0.5),
        ("e/w", 1),
    ],
    "field_note": [
        ("field note", 3),
        ("action:", 2),
        ("losses", 1),
        ("stuck pipe", 1),
        ("kick", 1),
        ("npt", 0.5),
    ],
}
FILENAME = {
    "_wcr": "wcr",
    "completion": "wcr",
    "_ddr": "ddr",
    "daily": "ddr",
    "_survey": "survey_report",
    "survey": "survey_report",
    "_note": "field_note",
}


def classify(
    text: str, filename: str = "", registered: str | None = None
) -> tuple[str, float, dict[str, float]]:
    """(doc_type, confidence 0-1, scores). With no readable text, file name and registered
    type decide, at low confidence."""
    t = re.sub(r"\s+", " ", text.lower())
    scores = {k: sum(w for kw, w in kws if kw in t) for k, kws in KEYWORDS.items()}
    name = filename.lower()
    for hint, kind in FILENAME.items():
        if hint in name:
            scores[kind] += 2
            break
    if registered in scores:
        scores[registered] += 1
    best = max(scores, key=lambda k: scores[k])
    total = sum(scores.values())
    if scores[best] == 0:
        return "other", 0.0, scores
    conf = scores[best] / total if t.strip() else min(0.5, scores[best] / total)
    return best, round(conf, 3), scores
