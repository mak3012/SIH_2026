"""Alert metrics from replayed wells.

Definitions (docs/evaluation.md repeats them):

- Event: a drilling problem recorded in the well's DDR / WCR (the `event` table). In the
  replay it happens when the bit reaches its depth; its start is the replay time at which the
  hazard episode begins (what the DDR time log records as the start of the problem).
- Detected: a delivered alert for the same hazard raised from MATCH_BEFORE before the event's
  start until MATCH_AFTER after the episode ends. Reported at advisory+ and warning+.
- Lead time: event start minus the first matching alert, in hours; positive = warned before
  the event started, negative = detected after it started.
- Decision window: the event's recorded end minus the first matching alert, in hours: how
  long the crew had, after the first alert, while the problem was still developing.
- False alert: a delivered alert (raise or reopen) that matches no event of its hazard.
  Rate per 24 operating hours (every rig state except static / unknown).
- Brier score: mean (posterior - label)^2 over every evaluation (every 5 s of data) of every
  hazard, label 1 inside an episode. Skill = 1 - Brier / Brier of always predicting the base
  rate. Calibration: evaluations binned by posterior, mean posterior vs observed frequency.
"""

import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from api.realtime.features import HAZARDS

MATCH_BEFORE = timedelta(hours=6)
MATCH_AFTER = timedelta(minutes=30)
BINS = [0.0, 0.001, 0.01, 0.05, 0.1, 0.2, 0.5, 0.8, 1.0000001]


def _ts(v: Any) -> datetime:
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v))


def well_metrics(r: dict[str, Any]) -> dict[str, Any]:
    eps = r["episodes"]
    alerts = [a for a in r["alerts"] if a["kind"] in ("raise", "reopen", "escalate")]
    rows = []
    matched_keys: set[tuple[str, int]] = set()
    for e in eps:
        lo, hi = _ts(e["onset"]) - MATCH_BEFORE, _ts(e["end"]) + MATCH_AFTER
        hit = sorted(
            (a for a in alerts if a["hazard"] == e["hazard"] and lo <= _ts(a["ts"]) <= hi),
            key=lambda a: _ts(a["ts"]),
        )
        matched_keys |= {(a["hazard"], a["key"]) for a in hit}
        first = hit[0] if hit else None
        warn = next((a for a in hit if a["tier"] >= 2), None)
        rows.append(
            {
                "well": r["well"],
                "hazard": e["hazard"],
                "onset": e["onset"],
                "md": e["md"],
                "detected": first is not None,
                "detected_warning": warn is not None,
                "lead_h": None
                if first is None
                else (_ts(e["onset"]) - _ts(first["ts"])).total_seconds() / 3600,
                "lead_warning_h": None
                if warn is None
                else (_ts(e["onset"]) - _ts(warn["ts"])).total_seconds() / 3600,
                "window_h": None
                if first is None
                else (_ts(e["end"]) - _ts(first["ts"])).total_seconds() / 3600,
                "first_tier": None if first is None else max(a["tier"] for a in hit),
            }
        )
    false = [
        a
        for a in alerts
        if a["kind"] in ("raise", "reopen") and (a["hazard"], a["key"]) not in matched_keys
    ]
    return {
        "well": r["well"],
        "events": rows,
        "false_alerts": false,
        "operating_hours": r["operating_hours"],
    }


def labels_at(eps: list[dict[str, Any]]) -> dict[str, list[tuple[datetime, datetime]]]:
    out: dict[str, list[tuple[datetime, datetime]]] = defaultdict(list)
    for e in eps:
        out[e["hazard"]].append((_ts(e["onset"]), _ts(e["end"])))
    return out


def scores(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Brier score, skill and calibration bins per hazard and overall."""
    pairs: dict[str, list[tuple[float, int]]] = defaultdict(list)
    for r in results:
        lab = labels_at(r["episodes"])
        for row in r["timeline"]:
            t = _ts(row["ts"])
            for h, p in row["post"].items():
                y = int(any(a <= t <= b for a, b in lab.get(h, [])))
                pairs[h].append((p, y))
    out: dict[str, Any] = {}
    allp = [x for v in pairs.values() for x in v]
    for h, v in [*sorted(pairs.items()), ("all", allp)]:
        if not v:
            continue
        n = len(v)
        base = sum(y for _, y in v) / n
        brier = sum((p - y) ** 2 for p, y in v) / n
        ref = base * (1 - base)
        cal = []
        for lo, hi in zip(BINS, BINS[1:], strict=False):
            b = [(p, y) for p, y in v if lo <= p < hi]
            if b:
                cal.append(
                    {
                        "lo": lo,
                        "hi": min(hi, 1.0),
                        "n": len(b),
                        "mean_p": sum(p for p, _ in b) / len(b),
                        "observed": sum(y for _, y in b) / len(b),
                    }
                )
        out[h] = {
            "n": n,
            "base_rate": base,
            "brier": brier,
            "brier_ref": ref,
            "skill": (1 - brier / ref) if ref > 0 else None,
            "calibration": cal,
        }
    return out


def aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    per_well = [well_metrics(r) for r in results]
    events = [e for w in per_well for e in w["events"]]
    false = [a for w in per_well for a in w["false_alerts"]]
    hours = sum(w["operating_hours"] for w in per_well)

    def summary(evs: list[dict[str, Any]], fa: list[dict[str, Any]]) -> dict[str, Any]:
        leads = [e["lead_h"] for e in evs if e["lead_h"] is not None]
        leads_w = [e["lead_warning_h"] for e in evs if e["lead_warning_h"] is not None]
        windows = [e["window_h"] for e in evs if e["window_h"] is not None]
        n = len(evs)
        return {
            "events": n,
            "detected": sum(e["detected"] for e in evs),
            "detected_warning": sum(e["detected_warning"] for e in evs),
            "recall": sum(e["detected"] for e in evs) / n if n else None,
            "recall_warning": sum(e["detected_warning"] for e in evs) / n if n else None,
            "median_lead_h": statistics.median(leads) if leads else None,
            "median_lead_warning_h": statistics.median(leads_w) if leads_w else None,
            "warned_before_start": sum(1 for x in leads if x > 0),
            "median_window_h": statistics.median(windows) if windows else None,
            "false_alerts": len(fa),
            "false_per_24h": len(fa) / hours * 24 if hours else None,
        }

    by_hazard = {
        h: summary([e for e in events if e["hazard"] == h], [a for a in false if a["hazard"] == h])
        for h in HAZARDS
    }
    return {
        "wells": len(results),
        "replay_hours": sum(r["hours"] for r in results),
        "lost_samples": sum(r.get("lost", 0) for r in results),
        "operating_hours": hours,
        "overall": summary(events, false),
        "by_hazard": {h: v for h, v in by_hazard.items() if v["events"] or v["false_alerts"]},
        "scores": scores(results),
        "per_well": per_well,
    }
