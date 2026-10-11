"""LLM explainer for alerts: writes the "why", citing offset evidence (document, page).

Runs AFTER the alert is stored and delivered (service.py queues it); it can only add text.
Its output is verified like a briefing: every number must appear in the alert's evidence (at
the written precision), every citation must be one of the listed ids. One retry with the
problems; otherwise the explanation is dropped (status `failed`) and the alert is unchanged.
"""

import json
import re
from typing import Any

from api.intelligence.linter import NUMBER
from api.intelligence.linter import _matches as matches
from core.llm import LLM
from core.telemetry import VERIFIER_REJECTS

SCHEMA = {
    "type": "object",
    "properties": {
        "why": {"type": "string"},
        "citations": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["why", "citations"],
}


def _numbers(x: Any) -> list[float]:
    if isinstance(x, bool) or x is None:
        return []
    if isinstance(x, int | float):
        return [abs(float(x))]
    if isinstance(x, dict):
        return [v for y in x.values() for v in _numbers(y)]
    if isinstance(x, list | tuple):
        return [v for y in x for v in _numbers(y)]
    return []


def refs(alert: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {f"R{i}": e for i, e in enumerate(alert["evidence"].get("roadmap_evidence", []), 1)}


def variables(alert: dict[str, Any], feedback: str = "") -> dict[str, str]:
    ev = alert["evidence"]
    head = {k: alert[k] for k in ("hazard", "tier_name", "prior", "lr", "posterior")}
    head |= {
        "rig_state": ev.get("rig_state"),
        "formation": ev.get("formation"),
        "bit_md": ev.get("bit_md"),
    }
    return {
        "alert": json.dumps(head),
        "features": json.dumps({**ev.get("features", {}), "physics": ev.get("physics")}),
        "prior": json.dumps(ev.get("prior", {})),
        "evidence": json.dumps(refs(alert)) if refs(alert) else "none",
        "feedback": feedback,
    }


def verify(out: dict[str, Any], alert: dict[str, Any]) -> list[str]:
    problems = []
    rs = refs(alert)
    for c in out.get("citations", []):
        if c not in rs:
            problems.append(f"unknown citation {c}")
    for c in re.findall(r"\[(R\d+)\]", out["why"]):
        if c not in rs:
            problems.append(f"unknown citation {c} in text")
    allowed = _numbers(alert["evidence"]) + _numbers(
        [alert["prior"], alert["lr"], alert["posterior"]]
    )
    allowed += [v * 100 for v in list(allowed) if 0 <= v <= 1]
    text = out["why"]
    for name in {str(e.get("well")) for e in rs.values()} | {
        str(e.get("document")) for e in rs.values()
    }:
        text = re.sub(rf"(?<![\w.]){re.escape(name)}(?![\w])", " ", text)
    text = re.sub(r"\[R\d+\]", " ", text)
    for tok in NUMBER.findall(text):
        if not matches(tok, allowed):
            problems.append(f"number {tok} is not in the alert's evidence")
    if not out["why"].strip():
        problems.append("empty explanation")
    return problems


def explain(llm: LLM, alert: dict[str, Any]) -> dict[str, Any]:
    """{'status': 'done'|'failed', 'why', 'citations', 'model', 'problems'}. Raises on
    transport errors (the service records them as failed)."""
    feedback, problems = "", []
    for _ in range(2):
        out = llm.complete_json(
            "alert_explainer", variables(alert, feedback), SCHEMA, alert_id=alert["id"]
        )
        if out is None:
            return {"status": "failed", "problems": ["no valid output from the model"]}
        problems = verify(out, alert)
        if problems:
            VERIFIER_REJECTS.labels("alert_explainer").inc()
        else:
            rs = refs(alert)
            return {
                "status": "done",
                "why": out["why"].strip(),
                "citations": [{"id": c, **rs[c]} for c in out["citations"]],
                "model": llm.model_version(llm.cfg.model),
                "problems": [],
            }
        feedback = "Your previous answer had problems: " + "; ".join(problems) + ". Fix them."
    return {"status": "failed", "problems": problems}
