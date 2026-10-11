"""Replay evaluation: windows, episodes, metrics, report rendering, placeholders; and one held-out
well end to end through the live path against a database (TEST_DATABASE_URL)."""

import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest

from api.replay import metrics, report
from api.replay.runner import episodes, windows

T = datetime(2030, 1, 1, tzinfo=UTC)


def at(minutes: float) -> datetime:
    return T + timedelta(minutes=minutes)


def test_windows_merge_and_quiet_wells() -> None:
    assert windows([("loss", 1000.0), ("kick", 1100.0), ("loss", 2000.0)], 500, 3000) == [
        (880.0, 1140.0),
        (1880.0, 2040.0),
    ]
    assert windows([("loss", 520.0)], 500, 3000) == [(500.0, 560.0)]  # never above the start
    assert windows([], 500, 3000) == [(1625.0, 1875.0)]  # no events: a mid-well section


def test_episodes_from_labels() -> None:
    rows = [
        (at(i), {"bit_md": 1000 + i}, frozenset({"loss"} if 3 <= i <= 5 else set()))
        for i in range(9)
    ]
    eps = episodes(rows)
    assert [(e.hazard, e.onset, e.end, e.md) for e in eps] == [("loss", at(3), at(5), 1003.0)]


def result(
    alerts: list[dict[str, Any]], eps: list[dict[str, Any]], hours: float = 24.0
) -> dict[str, Any]:
    timeline = [
        {"ts": at(m), "post": {"loss": 0.9 if 60 <= m <= 70 else 0.001, "kick": 0.0005}}
        for m in range(0, 120)
    ]
    return {
        "wellbore_id": 1,
        "well": "W",
        "episodes": eps,
        "alerts": alerts,
        "timeline": timeline,
        "hours": hours,
        "operating_hours": hours,
    }


def test_detection_lead_time_and_false_alerts() -> None:
    eps = [{"hazard": "loss", "onset": at(60), "end": at(70), "md": 1500.0}]
    alerts = [
        {"hazard": "loss", "ts": at(61), "tier": 1, "kind": "raise", "key": 1, "posterior": 0.3},
        {"hazard": "loss", "ts": at(62), "tier": 3, "kind": "escalate", "key": 1, "posterior": 0.9},
        {"hazard": "kick", "ts": at(30), "tier": 2, "kind": "raise", "key": 2, "posterior": 0.6},
        {
            "hazard": "loss",
            "ts": at(60 * 12),
            "tier": 1,
            "kind": "raise",
            "key": 3,
            "posterior": 0.3,
        },
    ]
    w = metrics.well_metrics(result(alerts, eps))
    e = w["events"][0]
    assert e["detected"] and e["detected_warning"] and e["first_tier"] == 3
    assert e["lead_h"] == pytest.approx(-1 / 60) and e["lead_warning_h"] == pytest.approx(-2 / 60)
    assert sorted(a["key"] for a in w["false_alerts"]) == [2, 3]  # wrong hazard; far too late
    agg = metrics.aggregate([result(alerts, eps)])
    assert agg["overall"]["recall_warning"] == 1.0
    assert agg["overall"]["false_per_24h"] == pytest.approx(2.0)
    early = [
        {"hazard": "loss", "ts": at(30), "tier": 2, "kind": "raise", "key": 9, "posterior": 0.6}
    ]
    assert metrics.well_metrics(result(early, eps))["events"][0]["lead_h"] == pytest.approx(0.5)
    missed = metrics.well_metrics(result([], eps))["events"][0]
    assert not missed["detected"] and missed["lead_h"] is None


def test_brier_and_calibration() -> None:
    eps = [{"hazard": "loss", "onset": at(60), "end": at(70), "md": 1500.0}]
    s = metrics.scores([result([], eps)])
    loss = s["loss"]
    assert loss["n"] == 120 and loss["base_rate"] == pytest.approx(11 / 120)
    assert loss["brier"] == pytest.approx((11 * 0.1**2 + 109 * 0.001**2) / 120)
    assert loss["skill"] > 0.9
    hi = next(c for c in loss["calibration"] if c["lo"] == 0.8)
    assert hi["n"] == 11 and hi["observed"] == 1.0
    assert s["kick"]["skill"] is None  # no kick ever: no reference to beat


def test_report_files_and_placeholders(tmp_path: Path) -> None:
    eps = [{"hazard": "loss", "onset": at(60), "end": at(70), "md": 1500.0}]
    alerts = [
        {"hazard": "loss", "ts": at(61), "tier": 2, "kind": "raise", "key": 1, "posterior": 0.6}
    ]
    r = {
        **result(alerts, eps),
        "offsets": [("X-1", 0.5)],
        "windows": [(1400.0, 1540.0)],
        "timeline": [
            {**row, "prior": {"loss": 0.001, "kick": 0.0005}, "gated": ["loss"]}
            for row in result(alerts, eps)["timeline"]
        ],
    }
    agg = metrics.aggregate([r])
    extraction = {"typed": {"docs": 14, "readable": 14, "precision": 1.0, "recall": 1.0, "f1": 1.0}}
    chat = {
        "rules": {"questions": 100, "accuracy": 1.0, "citation": 1.0, "verifier_reject_rate": 0.0}
    }
    meta = {"generated": "now", "command": "test", "detectors": "test"}
    hl = report.write_report(tmp_path, agg, [r], extraction, chat, meta)
    for name in ("report.html", "report.pdf", "metrics.json", "recall.png", "calibration.png"):
        assert (tmp_path / name).stat().st_size > 500, name
    wells = json.loads((tmp_path / "wells.json").read_text(encoding="utf-8"))
    assert wells[0]["well"] == "W" and wells[0]["events"][0]["detected"]
    tl = json.loads((tmp_path / "timelines" / "W.json").read_text(encoding="utf-8"))
    assert len(tl["t"]) == len(tl["md"]) == len(tl["post"]["loss"]) <= report.TIMELINE_POINTS
    assert max(tl["post"]["loss"]) == 0.9  # bucket maxima keep the threshold crossing
    assert tl["alerts"][0]["t"] == 61 * 60 and tl["episodes"][0]["start"] == 60 * 60
    html_text = (tmp_path / "report.html").read_text(encoding="utf-8")
    assert "data:image/png;base64," in html_text and "W timeline" in html_text
    assert hl["alert_event_recall_warning"] == 1.0 and hl["chat_rules_accuracy"] == 1.0
    page = tmp_path / "deck.md"
    page.write_text(
        "Recall [measured:alert_event_recall_warning], chat [measured:chat_rules_accuracy], "
        "x [measured:nope], y [measured]\n",
        encoding="utf-8",
    )
    rep = report.fill([page], hl)
    assert page.read_text(encoding="utf-8").startswith("Recall 100%, chat 100%, x [measured:nope]")
    assert rep["replaced"] == 2 and rep["unknown_keys"] and rep["bare"]
    md = report.markdown(agg, [r], extraction, chat, meta, "eval-report")
    doc = tmp_path / "evaluation.md"
    doc.write_text(
        "a\n<!-- headline:start -->\nold\n<!-- headline:end -->\nb\n<!-- replay:start -->\n"
        "<!-- replay:end -->\n",
        encoding="utf-8",
    )
    report.write_docs(doc, md)
    text = doc.read_text(encoding="utf-8")
    assert "\nold\n" not in text and "Alert event recall" in text and "| W |" in text


def test_extraction_numbers_come_from_the_generated_table() -> None:
    ex = report.extraction_from_docs(Path(__file__).resolve().parents[3] / "docs" / "evaluation.md")
    assert ex is not None and {"typed", "scanned"} <= set(ex)
    assert 0 <= ex["scanned"]["precision"] <= 1


# ------------------------------------------------------------------ database: one held-out well


@pytest.fixture(scope="module")
def truth(db_url: str) -> str:
    from synthetic_assam.load_truth import load

    with psycopg.connect(db_url) as conn:
        load(conn, 8, 1)
    return db_url


def test_one_well_through_the_live_path(truth: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from api.intelligence import repo
    from api.replay.priors import loo_priors
    from api.replay.runner import replay_well

    monkeypatch.setenv("DATABASE_URL", truth)
    with psycopg.connect(truth) as conn:
        wb, name = conn.execute(
            "SELECT b.id, w.name FROM wellbore b JOIN well w ON w.id = b.well_id"
            " WHERE EXISTS (SELECT 1 FROM event e WHERE e.wellbore_id = b.id AND e.type = 'loss')"
            " ORDER BY w.name LIMIT 1"
        ).fetchone()  # type: ignore[misc]
        loo = loo_priors(conn, wb)
        assert loo.offsets and name not in [n for n, _ in loo.offsets]
        # The held-out well's own losses are not in the basin rates.
        with_it, without = repo.basin_rates(conn), repo.basin_rates(conn, exclude_wellbores=[wb])
        assert any(with_it[k] != without.get(k) for k in with_it if k[1] == "loss")

    factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    with asyncio.Runner(loop_factory=factory) as runner:
        r = runner.run(replay_well(truth, wb, None, None)).json()
    assert r["samples"] > 1000 and r["timeline"] and r["episodes"]
    assert {e["hazard"] for e in r["episodes"]} <= {e["type"] for e in r["events"]}
    with psycopg.connect(truth) as conn:
        stored = conn.execute(
            "SELECT count(*) FROM rt_measurement WHERE wellbore_id = %s", (wb,)
        ).fetchone()
        assert (
            stored is not None and stored[0] == r["samples"]
        )  # every sample went through the store
        n_alerts = conn.execute(
            "SELECT count(*) FROM alert WHERE wellbore_id = %s AND rule_id LIKE 'RT-%%'"
            " AND suppressed IS NULL",
            (wb,),
        ).fetchone()
    raised = sum(1 for a in r["alerts"] if a["kind"] == "raise")
    assert n_alerts is not None and n_alerts[0] == raised
    w = metrics.well_metrics(r)
    assert len(w["events"]) == len(r["episodes"])
    # A replay of a real (non-synthetic) wellbore is refused before anything is deleted.
    with psycopg.connect(truth) as conn:
        conn.execute(
            "UPDATE document SET synthetic = false WHERE title = %s", (f"{name} WCR (SYNTHETIC)",)
        )
        conn.commit()
    with pytest.raises(PermissionError), asyncio.Runner(loop_factory=factory) as runner:
        runner.run(replay_well(truth, wb, None, None))
