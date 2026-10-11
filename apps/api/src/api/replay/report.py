"""Evaluation report: charts (matplotlib), a self-contained HTML page, a PDF, the generated
sections of docs/evaluation.md, and metrics.json with the headline numbers (the values that
replace `[measured:<key>]` placeholders, see `python -m api.replay fill`)."""

import base64
import html
import io
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from api.realtime.fusion import AlarmPolicy  # noqa: E402

COLORS = {
    "loss": "#1f77b4",
    "kick": "#d62728",
    "stuck_pipe": "#9467bd",
    "pack_off": "#ff7f0e",
    "washout": "#2ca02c",
    "tight_hole": "#8c564b",
}
TIER = {1: "#e3b505", 2: "#f07f09", 3: "#c0392b"}


def _png(fig: Any) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def _pct(x: float | None) -> str:
    return "–" if x is None else f"{x:.0%}"


def _num(x: float | None, fmt: str = "{:.2f}") -> str:
    return "–" if x is None else fmt.format(x)


# ------------------------------------------------------------------ charts


def chart_recall(agg: dict[str, Any]) -> bytes:
    hz = list(agg["by_hazard"])
    fig, ax = plt.subplots(figsize=(6.4, 3.0))
    xs = range(len(hz))
    r1 = [agg["by_hazard"][h]["recall"] or 0 for h in hz]
    r2 = [agg["by_hazard"][h]["recall_warning"] or 0 for h in hz]
    ax.bar([x - 0.2 for x in xs], r1, 0.4, label="advisory or higher", color="#9ab8d8")
    ax.bar([x + 0.2 for x in xs], r2, 0.4, label="warning or higher", color="#1f4e79")
    for x, h in zip(xs, hz, strict=True):
        ax.text(x, 1.02, f"n={agg['by_hazard'][h]['events']}", ha="center", fontsize=8)
    ax.set_xticks(list(xs), [h.replace("_", " ") for h in hz])
    ax.set_ylim(0, 1.12)
    ax.set_ylabel("event recall")
    ax.legend(fontsize=8, loc="lower right")
    ax.set_title("Events detected, by hazard")
    return _png(fig)


def chart_lead(agg: dict[str, Any]) -> bytes:
    fig, ax = plt.subplots(figsize=(6.4, 2.8))
    rows = [e for w in agg["per_well"] for e in w["events"] if e["lead_h"] is not None]
    hz = sorted({e["hazard"] for e in rows})
    for i, h in enumerate(hz):
        ys = [e["lead_h"] * 60 for e in rows if e["hazard"] == h]
        ax.scatter([i] * len(ys), ys, color=COLORS.get(h, "k"), alpha=0.7, s=18)
    ax.axhline(0, color="k", lw=0.8)
    ax.set_xticks(range(len(hz)), [h.replace("_", " ") for h in hz])
    ax.set_ylabel("lead time (min)\n< 0: after the start")
    ax.set_title("First alert relative to the recorded start of the event")
    return _png(fig)


def chart_calibration(scores: dict[str, Any]) -> bytes:
    fig, ax = plt.subplots(figsize=(4.6, 4.2))
    floor = 1e-5
    for h, s in scores.items():
        cal = [c for c in s["calibration"] if c["n"] >= 30]
        if not cal or not s["base_rate"]:  # a hazard that never happened has nothing to calibrate
            continue
        xs = [max(c["mean_p"], floor) for c in cal]
        ys = [max(c["observed"], floor) for c in cal]
        style: dict[str, Any] = (
            {"color": "k", "lw": 2, "marker": "o"}
            if h == "all"
            else {"color": COLORS.get(h, "grey"), "lw": 1, "marker": ".", "alpha": 0.7}
        )
        ax.plot(xs, ys, label=h.replace("_", " "), **style)
    ax.plot([floor, 1], [floor, 1], ls="--", color="grey", lw=0.8)
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlim(floor, 1.2)
    ax.set_ylim(floor, 1.2)
    ax.set_xlabel("predicted probability (posterior)")
    ax.set_ylabel("observed frequency")
    ax.set_title("Calibration (bins with ≥ 30 evaluations)")
    ax.legend(fontsize=7)
    return _png(fig)


def chart_timeline(r: dict[str, Any]) -> bytes:
    """Risk score per hazard over the replayed windows (gaps between windows removed)."""
    rows = r["timeline"][::6]  # every 30 s
    if not rows:
        fig, ax = plt.subplots(figsize=(7, 1))
        ax.text(0.5, 0.5, "no evaluations", ha="center")
        return _png(fig)

    def ts(v: Any) -> datetime:
        return v if isinstance(v, datetime) else datetime.fromisoformat(str(v))

    # Compress the two-hour gaps between windows into a small separator.
    xs, shift, last = [], 0.0, None
    t0 = ts(rows[0]["ts"])
    breaks = []
    for row in rows:
        t = ts(row["ts"])
        if last is not None and (t - last).total_seconds() > 3600:
            shift += (t - last).total_seconds() / 3600 - 0.2
            breaks.append((t - t0).total_seconds() / 3600 - shift - 0.1)
        xs.append((t - t0).total_seconds() / 3600 - shift)
        last = t

    def x_of(v: Any) -> float:
        t = ts(v)
        best = min(range(len(rows)), key=lambda i: abs((ts(rows[i]["ts"]) - t).total_seconds()))
        return xs[best]

    shown = {e["hazard"] for e in r["episodes"]} | {a["hazard"] for a in r["alerts"]}
    fig, ax = plt.subplots(figsize=(9.5, 3.2))
    floor = 1e-6
    for h in sorted({h for row in rows for h in row["post"]}):
        ys = [max(row["post"].get(h, floor), floor) for row in rows]
        if h in shown:
            ax.plot(xs, ys, color=COLORS.get(h, "k"), lw=1.2, label=f"{h.replace('_', ' ')} risk")
            pr = [max(row["prior"].get(h, floor), floor) for row in rows]
            ax.plot(
                xs,
                pr,
                color=COLORS.get(h, "k"),
                lw=0.8,
                ls=":",
                label=f"{h.replace('_', ' ')} prior",
            )
        else:
            ax.plot(xs, ys, color="#bbbbbb", lw=0.5)
    for th, name in zip(AlarmPolicy().thresholds, ("advisory", "warning", "critical"), strict=True):
        ax.axhline(th, color="grey", ls="--", lw=0.7)
        ax.text(xs[-1], th, f" {name}", va="center", fontsize=7, color="grey")
    for i, e in enumerate(r["episodes"]):
        ax.axvspan(
            x_of(e["onset"]), x_of(e["end"]) + 0.02, color=COLORS.get(e["hazard"], "k"), alpha=0.18
        )
        y = (1.3, 6.0)[i % 2]  # stagger labels of neighbouring events
        ax.text(
            x_of(e["onset"]),
            y,
            f"{e['hazard'].replace('_', ' ')} (DDR)",
            fontsize=7,
            color=COLORS.get(e["hazard"], "k"),
        )
    for a in r["alerts"]:
        ax.scatter(
            [x_of(a["ts"])],
            [max(a["posterior"], floor)],
            marker="v",
            s=40,
            color=TIER.get(a["tier"], "k"),
            zorder=5,
            edgecolor="k",
            linewidth=0.4,
        )
    for b in breaks:
        ax.axvline(b, color="k", lw=0.6, ls="-.")
    ax.set_yscale("log")
    ax.set_ylim(floor, 20)
    ax.set_xlabel("replay hours (windows around the recorded events; -·- = feed gap)")
    ax.set_ylabel("probability")
    ax.set_title(
        f"{r['well']}: risk score, thresholds, alerts (▼ advisory / warning / critical), events"
    )
    if ax.get_legend_handles_labels()[0]:  # wells without events or alerts have none
        ax.legend(fontsize=6, loc="lower left", ncol=3)
    return _png(fig)


# ------------------------------------------------------------------ numbers


def extraction_from_docs(evaluation_md: Path) -> dict[str, dict[str, float]] | None:
    """Precision / recall by render from the table `make eval` writes into evaluation.md."""
    text = evaluation_md.read_text(encoding="utf-8")
    out = {}
    for m in re.finditer(
        r"^\| (typed|scanned|fax|handwritten) \| (\d+) \| (\d+) \| ([\d.]+) \| ([\d.]+) \| \*\*([\d.]+)\*\* \|$",
        text,
        re.M,
    ):
        out[m[1]] = {
            "docs": int(m[2]),
            "readable": int(m[3]),
            "precision": float(m[4]),
            "recall": float(m[5]),
            "f1": float(m[6]),
        }
    return out or None


def headline(
    agg: dict[str, Any], extraction: dict[str, Any] | None, chat: dict[str, Any] | None
) -> dict[str, Any]:
    o, s = agg["overall"], agg["scores"].get("all", {})
    h: dict[str, Any] = {
        "replay_wells": agg["wells"],
        "replay_operating_hours": round(agg["operating_hours"], 1),
        "alert_events": o["events"],
        "alert_event_recall": o["recall"],
        "alert_event_recall_warning": o["recall_warning"],
        "alert_median_lead_time_h": o["median_lead_h"],
        "alert_median_lead_time_min": None
        if o["median_lead_h"] is None
        else o["median_lead_h"] * 60,
        "alert_warned_before_start": o["warned_before_start"],
        "alert_median_decision_window_min": None
        if o["median_window_h"] is None
        else o["median_window_h"] * 60,
        "alert_false_per_24h": o["false_per_24h"],
        "alert_brier": s.get("brier"),
        "alert_brier_skill": s.get("skill"),
    }
    for render, v in (extraction or {}).items():
        h[f"extraction_{render}_precision"] = v["precision"]
        h[f"extraction_{render}_recall"] = v["recall"]
        h[f"extraction_{render}_f1"] = v["f1"]
    if chat:
        for mode, rep in chat.items():
            h[f"chat_{mode}_accuracy"] = rep["accuracy"]
            h[f"chat_{mode}_citation"] = rep["citation"]
            h[f"chat_{mode}_verifier_reject_rate"] = rep["verifier_reject_rate"]
    return h


def fmt_value(key: str, v: Any) -> str:
    if v is None:
        return "not measured"
    shares = ("recall", "recall_warning", "precision", "f1", "accuracy", "citation", "reject_rate")
    if key.endswith(shares):
        return f"{float(v):.0%}"
    if key.endswith("skill"):
        return f"{float(v):.2f}"
    if key.endswith("window_min"):
        return f"{v:.1f} min"
    if key.endswith("_min"):
        return f"{v:+.1f} min"
    if key.endswith("_h"):
        return f"{v:+.2f} h"
    if key.endswith("per_24h"):
        return f"{v:.2f}"
    if key == "alert_brier":
        return f"{v:.5f}"
    return f"{v:g}" if isinstance(v, float) else str(v)


TIMELINE_POINTS = 1500


def timeline(r: dict[str, Any], w: dict[str, Any]) -> dict[str, Any]:
    """One well's replay for the Replay Studio, columnar and thinned to at most
    TIMELINE_POINTS buckets (the highest risk score per hazard in each bucket, so a threshold
    crossing is never thinned away). Times are seconds from the first evaluation."""
    rows = r["timeline"]
    if not rows:
        return {"well": r["well"], "t": [], "post": {}}
    t0 = datetime.fromisoformat(str(rows[0]["ts"]))

    def sec(ts: Any) -> float:
        return round((datetime.fromisoformat(str(ts)) - t0).total_seconds(), 1)

    span = sec(rows[-1]["ts"]) or 1.0
    step = max(30.0, span / TIMELINE_POINTS)
    hazards = sorted({h for row in rows for h in row["post"]})
    buckets: dict[int, dict[str, Any]] = {}
    for row in rows:
        b = buckets.setdefault(int(sec(row["ts"]) // step), {"post": {}})
        b.update(md=row.get("md"), state=row.get("state"), formation=row.get("formation"))
        for h, p in row["post"].items():
            b["post"][h] = max(p, b["post"].get(h, 0.0))
    keys = sorted(buckets)
    return {
        "well": r["well"],
        "wellbore_id": r["wellbore_id"],
        "start": rows[0]["ts"],
        "step_s": step,
        "t": [round(k * step, 1) for k in keys],
        "md": [buckets[k]["md"] for k in keys],
        "state": [buckets[k]["state"] for k in keys],
        "formation": [buckets[k]["formation"] for k in keys],
        "post": {h: [buckets[k]["post"].get(h, 0.0) for k in keys] for h in hazards},
        "thresholds": {"advisory": 0.2, "warning": 0.5, "critical": 0.8},
        "episodes": [
            {"hazard": e["hazard"], "start": sec(e["onset"]), "end": sec(e["end"]), "md": e["md"]}
            for e in r["episodes"]
        ],
        "alerts": [
            {
                "hazard": a["hazard"],
                "t": sec(a["ts"]),
                "tier": a["tier"],
                "kind": a["kind"],
                "posterior": a["posterior"],
            }
            for a in r["alerts"]
        ],
        "events": [
            {
                "hazard": e["hazard"],
                "md": e["md"],
                "detected": e["detected"],
                "detected_warning": e["detected_warning"],
                "lead_min": None if e["lead_h"] is None else round(e["lead_h"] * 60, 1),
                "window_min": None if e["window_h"] is None else round(e["window_h"] * 60, 1),
            }
            for e in w["events"]
        ],
        "false_alerts": [{"hazard": a["hazard"], "t": sec(a["ts"])} for a in w["false_alerts"]],
        "operating_hours": round(w["operating_hours"], 1),
    }


def well_summary(agg: dict[str, Any], results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per well, for the web app's Replay page (built in, like metrics.json)."""
    out = []
    for r, w in zip(results, agg["per_well"], strict=True):
        out.append(
            {
                "well": r["well"],
                "wellbore_id": r["wellbore_id"],
                "hours": round(r["hours"], 1),
                "operating_hours": round(w["operating_hours"], 1),
                "offsets": [n for n, _ in r["offsets"]],
                "false_alerts": len(w["false_alerts"]),
                "events": [
                    {
                        "hazard": e["hazard"],
                        "md": round(e["md"], 1),
                        "detected": e["detected"],
                        "detected_warning": e["detected_warning"],
                        "lead_min": None if e["lead_h"] is None else round(e["lead_h"] * 60, 1),
                        "window_min": None
                        if e["window_h"] is None
                        else round(e["window_h"] * 60, 1),
                    }
                    for e in w["events"]
                ],
            }
        )
    return out


# ------------------------------------------------------------------ markdown (docs/evaluation.md)


def markdown(
    agg: dict[str, Any],
    results: list[dict[str, Any]],
    extraction: dict[str, Any] | None,
    chat: dict[str, Any] | None,
    meta: dict[str, Any],
    img: str,
) -> dict[str, str]:
    o = agg["overall"]
    hl = headline(agg, extraction, chat)
    head = [
        f"Generated {meta['generated']} by `{meta['command']}` ({meta['detectors']}). "
        f"Full report: [HTML](eval-report/report.html), [PDF](eval-report/report.pdf); "
        f"numbers: [metrics.json](eval-report/metrics.json); per well: `eval-report/wells.json` and "
        f"the risk-score timelines the Replay studio plays (`eval-report/timelines/<well>.json`).",
        "",
        "| Measure | Value |",
        "|---|---|",
        f"| Alert event recall (warning or higher) | **{_pct(o['recall_warning'])}** of {o['events']} recorded events ({_pct(o['recall'])} advisory or higher) |",
        f"| Median lead time (first alert vs the event's recorded start) | **{fmt_value('alert_median_lead_time_min', hl['alert_median_lead_time_min'])}** ({o['warned_before_start']} of {o['detected']} detected events warned before they started) |",
        f"| Decision window (first alert to the event's recorded end) | **{_num(hl['alert_median_decision_window_min'], '{:.1f}')} min** median over {o['detected']} detected events |",
        f"| False alerts per 24 operating hours | **{_num(o['false_per_24h'])}** ({o['false_alerts']} in {agg['operating_hours']:.0f} h) |",
        f"| Brier score (all hazards, every evaluation) | **{_num(hl['alert_brier'], '{:.5f}')}**, skill {_num(hl['alert_brier_skill'], '{:.2f}')} vs always predicting the base rate |",
    ]
    for render in ("typed", "scanned", "fax", "handwritten"):
        if extraction and render in extraction:
            v = extraction[render]
            head.append(
                f"| Extraction, {render} documents | precision **{v['precision']:.3f}**, recall **{v['recall']:.3f}** |"
            )
    for mode, rep in (chat or {}).items():
        head.append(
            f"| Assistant answer accuracy ({mode}) | **{_pct(rep['accuracy'])}** of {rep['questions']} questions, citations {_pct(rep['citation'])}, verifier reject rate {rep['verifier_reject_rate']:.1%} |"
        )
    if not chat or "llm" not in chat:
        head.append(
            "| Assistant with the configured LLM | not measured in this run (`--chat-llm`) |"
        )

    rp = [
        f"Leave-one-well-out over **{agg['wells']} synthetic wells**, "
        f"{agg['replay_hours']:.0f} h of replayed rig data ({agg['operating_hours']:.0f} operating hours); "
        f"samples lost between simulator and service: {agg.get('lost_samples', 0)}.",
        "",
        "### Per hazard",
        "",
        "| Hazard | Events | Detected (advisory+) | Detected (warning+) | Median lead (min) | Before start | False alerts | per 24 h |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for h, v in agg["by_hazard"].items():
        rp.append(
            f"| {h.replace('_', ' ')} | {v['events']} | {v['detected']} ({_pct(v['recall'])}) | "
            f"{v['detected_warning']} ({_pct(v['recall_warning'])}) | "
            f"{_num(None if v['median_lead_h'] is None else v['median_lead_h'] * 60, '{:+.1f}')} | "
            f"{v['warned_before_start']} | {v['false_alerts']} | {_num(v['false_per_24h'])} |"
        )
    rp += [
        "",
        f"![Recall by hazard]({img}/recall.png)",
        "",
        f"![Lead time]({img}/lead.png)",
        "",
        "### Calibration",
        "",
        "| Hazard | Evaluations | Base rate | Brier | Brier (base rate only) | Skill |",
        "|---|---|---|---|---|---|",
    ]
    for h, s in agg["scores"].items():
        rp.append(
            f"| {h.replace('_', ' ')} | {s['n']:,} | {s['base_rate']:.4f} | {s['brier']:.5f} | "
            f"{s['brier_ref']:.5f} | {_num(s['skill'])} |"
        )
    rp += [
        "",
        f"![Calibration]({img}/calibration.png)",
        "",
        "### Per well",
        "",
        "| Well | Offsets (leave-one-out) | Events | Detected (warning+) | Lead (min) | Decision window (min) | False alerts | Operating h |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r, w in zip(results, agg["per_well"], strict=True):
        leads = (
            ", ".join(
                _num(e["lead_h"] * 60, "{:+.1f}") for e in w["events"] if e["lead_h"] is not None
            )
            or "–"
        )
        windows = (
            ", ".join(_num(e["window_h"] * 60, "{:.1f}") for e in w["events"] if e["window_h"])
            or "–"
        )
        offs = ", ".join(n for n, _ in r["offsets"][:4]) + (" …" if len(r["offsets"]) > 4 else "")
        rp.append(
            f"| {r['well']} | {offs or '–'} | "
            f"{', '.join(e['hazard'].replace('_', ' ') for e in w['events']) or 'none'} | "
            f"{sum(e['detected_warning'] for e in w['events'])}/{len(w['events'])} | {leads} | {windows} | "
            f"{len(w['false_alerts'])} | {w['operating_hours']:.1f} |"
        )
    rp += [
        "",
        "Per-well timelines (risk score, thresholds, alerts, the recorded event) are in the "
        "[HTML report](eval-report/report.html).",
    ]
    return {"headline": "\n".join(head), "replay": "\n".join(rp)}


def write_docs(evaluation_md: Path, sections: dict[str, str]) -> None:
    text = evaluation_md.read_text(encoding="utf-8")
    for name, body in sections.items():
        a, b = f"<!-- {name}:start -->", f"<!-- {name}:end -->"
        if a not in text:
            raise SystemExit(f"{evaluation_md} has no {a} marker")
        head, rest = text.split(a, 1)
        text = head + a + "\n" + body + "\n" + b + rest.split(b, 1)[1]
    evaluation_md.write_text(text, encoding="utf-8")


# ------------------------------------------------------------------ HTML / PDF


def write_report(
    out: Path,
    agg: dict[str, Any],
    results: list[dict[str, Any]],
    extraction: dict[str, Any] | None,
    chat: dict[str, Any] | None,
    meta: dict[str, Any],
) -> dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True)
    charts = {
        "recall": chart_recall(agg),
        "lead": chart_lead(agg),
        "calibration": chart_calibration(agg["scores"]),
    }
    for k, v in charts.items():
        (out / f"{k}.png").write_bytes(v)
    timelines = {r["well"]: chart_timeline(r) for r in results}
    hl = headline(agg, extraction, chat)
    (out / "wells.json").write_text(json.dumps(well_summary(agg, results), indent=1, default=str))
    (out / "timelines").mkdir(exist_ok=True)
    for r, w in zip(results, agg["per_well"], strict=True):
        (out / "timelines" / f"{r['well']}.json").write_text(
            json.dumps(timeline(r, w), separators=(",", ":"), default=str)
        )
    (out / "metrics.json").write_text(
        json.dumps(
            {
                "generated": meta["generated"],
                "source": meta["command"],
                "synthetic": True,
                "metrics": hl,
            },
            indent=1,
            default=str,
        )
    )
    md = markdown(agg, results, extraction, chat, meta, "eval-report")

    def img(b: bytes, alt: str) -> str:
        return f'<img alt="{html.escape(alt)}" src="data:image/png;base64,{base64.b64encode(b).decode()}">'

    def table(md_text: str) -> str:
        lines = [x for x in md_text.splitlines() if x.startswith("|")]
        out_html, header = [], True
        for line in lines:
            if set(line.replace("|", "").strip()) <= {"-"}:
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            tag = "th" if header else "td"
            out_html.append(
                "<tr>" + "".join(f"<{tag}>{_md_inline(c)}</{tag}>" for c in cells) + "</tr>"
            )
            header = False
        return "<table>" + "".join(out_html) + "</table>"

    blocks = md["replay"].split("\n\n")
    tables = [b for b in blocks if b.startswith("|")]
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Evaluation report</title>
<style>
:root {{ --bg:#fff; --fg:#14181f; --muted:#5b6472; --line:#d9dde3; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#0f1216; --fg:#e6e9ee; --muted:#9aa3b0; --line:#2a303a; }}
  img {{ background:#fff; }} }}
body {{ margin:0 auto; max-width:1000px; padding:24px 16px; font:15px/1.5 system-ui,sans-serif;
  background:var(--bg); color:var(--fg); }}
h1 {{ font-size:24px; }} h2 {{ margin-top:32px; border-bottom:1px solid var(--line); }}
table {{ border-collapse:collapse; margin:8px 0 16px; font-size:13px; display:block; overflow-x:auto; }}
th, td {{ border:1px solid var(--line); padding:4px 8px; text-align:left; vertical-align:top; }}
img {{ max-width:100%; height:auto; }} .muted {{ color:var(--muted); }} .note {{ border-left:4px solid #e3b505; padding:4px 12px; }}
</style></head><body>
<h1>Well Intelligence: evaluation report</h1>
<p class="muted">{html.escape(meta["generated"])} · {html.escape(meta["command"])} · {html.escape(meta["detectors"])}</p>
<p class="note">All wells, documents and events here are <b>synthetic</b> (synthetic_assam). The numbers
measure the method on data built around its own physics; they are an upper bound, not field performance.</p>
<h2>Headline</h2>{table(md["headline"])}
<h2>Method</h2>
<p>Leave-one-well-out: for each held-out well, analogues and the hazard roadmap are built from the other
wells only (basin rates exclude the held-out well), the well's real-time data is generated from its survey,
casing and DDR-recorded events, served by the WITSML simulator, read by the WITSML poller and processed by
the live service (store, rig state, derived channels, detectors, fusion, alarm policy). Depth windows around
each recorded event are replayed (120 m above to 40 m below). An event is <b>detected</b> when an alert of
its hazard is raised from 6 h before its recorded start until 30 min after it ends; <b>lead time</b> is the
recorded start minus the first such alert (negative: detected after the start). <b>False alerts</b> match no
event; rate per 24 operating hours. <b>Brier</b>: mean squared error of the posterior against "inside an
event" over every 5-second evaluation.</p>
<h2>Alerts by hazard</h2>{table(tables[0])}
{img(charts["recall"], "recall by hazard")}{img(charts["lead"], "lead time")}
<h2>Calibration</h2>{table(tables[1])}{img(charts["calibration"], "calibration")}
<h2>Per well</h2>{table(tables[2])}
<h2>Timelines</h2>
<p class="muted">Risk score (posterior) per hazard, its prior (dotted), alert thresholds (dashed), alerts (▼,
colour = tier) and the recorded event (shaded). Grey lines: other hazards.</p>
{"".join(f"<h3>{html.escape(w)}</h3>" + img(b, f"{w} timeline") for w, b in timelines.items())}
</body></html>"""
    (out / "report.html").write_text(page, encoding="utf-8")
    write_pdf(out / "report.pdf", md, charts, timelines, meta)
    return hl


def _md_inline(s: str) -> str:
    s = html.escape(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    return re.sub(r"`(.+?)`", r"<code>\1</code>", s)


def write_pdf(
    path: Path,
    md: dict[str, str],
    charts: dict[str, bytes],
    timelines: dict[str, bytes],
    meta: dict[str, Any],
) -> None:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import (
        Image,
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    ss = getSampleStyleSheet()
    small = ss["BodyText"].clone("small", fontSize=7, leading=8.5)

    def table(md_text: str) -> Table:
        rows = []
        for line in md_text.splitlines():
            if not line.startswith("|") or set(line.replace("|", "").strip()) <= {"-"}:
                continue
            rows.append(
                [Paragraph(_md_inline(c.strip()), small) for c in line.strip("|").split("|")]
            )
        t = Table(rows, repeatRows=1)
        t.setStyle(
            TableStyle(
                [
                    ("GRID", (0, 0), (-1, -1), 0.3, colors.grey),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef1f5")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]
            )
        )
        return t

    def image(b: bytes, width: float) -> Image:
        from reportlab.lib.utils import ImageReader

        iw, ih = ImageReader(io.BytesIO(b)).getSize()
        return Image(io.BytesIO(b), width=width, height=width * ih / iw)

    blocks = [b for b in md["replay"].split("\n\n") if b.startswith("|")]
    story: list[Any] = [
        Paragraph("Well Intelligence: evaluation report", ss["Title"]),
        Paragraph(f"{meta['generated']} · {meta['command']} · {meta['detectors']}", small),
        Paragraph(
            "All data synthetic (synthetic_assam): an upper bound, not field performance.", small
        ),
        Spacer(1, 0.3 * cm),
        table(md["headline"]),
        Spacer(1, 0.4 * cm),
        Paragraph("Alerts by hazard (leave-one-well-out replay)", ss["Heading2"]),
        table(blocks[0]),
        image(charts["recall"], 14 * cm),
        image(charts["lead"], 14 * cm),
        PageBreak(),
        Paragraph("Calibration", ss["Heading2"]),
        table(blocks[1]),
        image(charts["calibration"], 10 * cm),
        PageBreak(),
        Paragraph("Per well", ss["Heading2"]),
        table(blocks[2]),
        PageBreak(),
        Paragraph("Timelines", ss["Heading2"]),
    ]
    for w, b in timelines.items():
        story += [Paragraph(w, ss["Heading4"]), image(b, 17.5 * cm)]
    SimpleDocTemplate(
        str(path),
        pagesize=A4,
        leftMargin=1.5 * cm,
        rightMargin=1.5 * cm,
        topMargin=1.5 * cm,
        bottomMargin=1.5 * cm,
        title="Evaluation report",
    ).build(story)


# ------------------------------------------------------------------ placeholders


PLACEHOLDER = re.compile(r"\[measured:([a-z0-9_]+)\]")


def fill(paths: list[Path], metrics: dict[str, Any]) -> dict[str, Any]:
    """Replace `[measured:<key>]` with the formatted value; report unknown keys and bare
    `[measured]` placeholders (which do not say which number they mean)."""
    report: dict[str, Any] = {"replaced": 0, "unknown_keys": [], "bare": []}
    for p in paths:
        text = p.read_text(encoding="utf-8")

        def sub(m: re.Match[str], p: Path = p) -> str:
            k = m.group(1)
            if k not in metrics:
                report["unknown_keys"].append(f"{p}:{k}")
                return m.group(0)
            report["replaced"] += 1
            return fmt_value(k, metrics[k])

        new = PLACEHOLDER.sub(sub, text)
        report["bare"] += [
            f"{p}:{text[: m.start()].count(chr(10)) + 1}" for m in re.finditer(r"\[measured\]", new)
        ]
        if new != text:
            p.write_text(new, encoding="utf-8")
    return report
