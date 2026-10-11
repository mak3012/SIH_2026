"""The extraction gold set: 50 synthetic documents with field-level ground truth, stratified by
document type and how the document was produced (typed, scanned, faxed, handwritten)."""

import random
from collections import defaultdict
from typing import Any

# (doc_type, render or None for any) -> how many. Sums to 50.
QUOTA: dict[tuple[str, str | None], int] = {
    ("wcr", "typed"): 5,
    ("wcr", "scanned"): 5,
    ("wcr", "fax"): 3,
    ("ddr", "typed"): 7,
    ("ddr", "scanned"): 8,
    ("ddr", "fax"): 5,
    ("survey_report", None): 5,
    ("field_note", "handwritten"): 12,
}
CONVENTIONS = {
    "value": "Canonical value of what is printed: depths/elevations in metres (a depth printed "
    "in feet is converted from the printed number), densities in sg, dates ISO 8601, event "
    "types as in the database enum. Coordinates are in the datum named by coordinate_datum.",
    "printed": "Exact string on the page.",
    "unit": "Unit the page printed the value in.",
    "page": "1-based page number.",
    "bbox": "[x0, y0, x1, y1] as fractions of page width/height, origin top-left, after any "
    "scan rotation (axis-aligned box around the rotated text).",
    "names": "List items are indexed: tops[2].md, time_breakdown[0].hours, events[1].type.",
}


def build_gold(docs: list[dict[str, Any]], seed: int, n: int = 50) -> dict[str, Any]:
    rng = random.Random(seed)
    pool = sorted(docs, key=lambda d: d["path"])
    buckets: dict[tuple[str, str | None], list[dict[str, Any]]] = defaultdict(list)
    for d in pool:
        key = (d["doc_type"], d["render"])
        buckets[key if key in QUOTA else (d["doc_type"], None)].append(d)
    chosen: list[dict[str, Any]] = []
    for key, want in QUOTA.items():
        have = buckets.get(key, [])
        chosen += rng.sample(have, min(want, len(have)))
    rest = [d for d in pool if d not in chosen]
    chosen += rng.sample(rest, min(n - len(chosen), len(rest)))  # fill any short bucket
    chosen.sort(key=lambda d: d["path"])
    return {
        "name": "synthetic_assam extraction gold set",
        "version": 1,
        "synthetic": True,
        "seed": seed,
        "count": len(chosen),
        "conventions": CONVENTIONS,
        "documents": [
            {
                "id": d["path"],
                **{
                    k: d[k]
                    for k in (
                        "path",
                        "doc_type",
                        "render",
                        "well",
                        "pages",
                        "depth_unit",
                        "sha256",
                        "vintage",
                        "fields",
                    )
                },
            }
            for d in chosen
        ],
    }
