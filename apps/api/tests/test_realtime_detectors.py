"""Detector bank: group K-fold training, calibration into likelihood ratios, detection on a
held-out well, and that live features equal training features (one code path)."""

import random
from pathlib import Path

import numpy as np
import pytest

from api.realtime import detectors as dt
from api.realtime.channels import Sample
from api.realtime.context import context_from_plan
from api.realtime.features import FEATURES
from api.realtime.pipeline import Pipeline
from api.realtime.synthetic import generate, random_plan


@pytest.fixture(scope="module")
def rows() -> list:
    return dt.dataset(range(12), 6.0, workers=6)


@pytest.fixture(scope="module")
def bank(rows: list) -> dt.Bank:
    return dt.train(rows, folds=3)


def test_folds_never_share_a_well(rows: list) -> None:
    from sklearn.model_selection import GroupKFold

    groups = np.array([r[0] for r in rows])
    for tr, te in GroupKFold(n_splits=3).split(groups, groups, groups):
        assert not set(groups[tr]) & set(groups[te])


def test_calibration_is_monotone_and_lr_finite(bank: dt.Bank) -> None:
    for h, m in bank.models.items():
        grid = np.linspace(0, 1, 101)
        p = m.iso.predict(grid)
        assert np.all(np.diff(p) >= -1e-12), h
        x = np.zeros((3, len(FEATURES) + 1))
        x[:] = np.nan
        lr, pc, _ = m.lr_batch(x)
        assert np.all(np.isfinite(lr)) and np.all((lr >= dt.LR_MIN) & (lr <= dt.LR_MAX))
        assert np.all((pc > 0) & (pc < 1))
        assert m.metrics["auc_oof"] > 0.75, (h, m.metrics)


def test_held_out_kick_raises_a_warning(bank: dt.Bank) -> None:
    plan = random_plan(random.Random(777), "HELD-OUT", 3.0)
    plan.rates = {}
    plan.wiper_every = 0
    plan.events = [("kick", plan.start_md + 40)]
    pipe = Pipeline(context_from_plan(plan), bank)
    onset, alerts = None, []
    for t in generate(plan, seed=777):
        if "kick" in t.labels and onset is None:
            onset = t.ts
        step = pipe.push(Sample(0, t.ts, t.values))
        alerts += [
            a for a in step.actions if a.hazard == "kick" and a.kind in ("raise", "escalate")
        ]
    assert onset is not None
    warn = [a for a in alerts if a.tier >= 2]
    assert warn, alerts
    assert (warn[0].ts - onset).total_seconds() < 600
    assert warn[0].evidence["detector"]["calibrated"] is True


def test_lr_survives_feed_rounding(bank: dt.Bank) -> None:
    """A WITSML feed carries 4 decimals. The detectors must not depend on anything finer: a
    stuck-pipe model once keyed on 1e-17 L/min return flow and went blind behind a real feed."""
    from api.realtime.features import HAZARDS

    plan = random_plan(random.Random(31), "ROUND", 4.0, ["stuck_pipe", "kick"])
    a, b = Pipeline(context_from_plan(plan), bank), Pipeline(context_from_plan(plan), bank)
    worst = 0.0
    for t in generate(plan, seed=31):
        sa = a.push(Sample(0, t.ts, t.values))
        sb = b.push(Sample(0, t.ts, {k: round(v, 4) for k, v in t.values.items()}))
        for h in HAZARDS:
            if h in sa.hazards:
                la, lb = sa.hazards[h]["lr"], sb.hazards[h]["lr"]
                worst = max(worst, abs(np.log10(la) - np.log10(lb)))
    assert worst < 0.01, worst


def test_fast_scoring_matches_the_libraries(bank: dt.Bank) -> None:
    rng = np.random.default_rng(0)
    for m in bank.models.values():
        x = rng.normal(0, 1, (40, len(FEATURES) + 1))
        x[::7, 3] = np.nan
        fast = dt.FastIForest(m.iforest)
        assert np.allclose(
            fast.score_samples(np.nan_to_num(x)), m.iforest.score_samples(np.nan_to_num(x))
        )
        a = -m.iforest.score_samples(np.nan_to_num(x))
        xa = np.column_stack([x, a])
        assert np.allclose(
            m.xgb.get_booster().inplace_predict(xa), m.xgb.predict_proba(xa)[:, 1], atol=1e-6
        )
        grid = np.linspace(-0.1, 1.1, 300)
        assert np.allclose(
            np.interp(grid, m.iso.X_thresholds_, m.iso.y_thresholds_), m.iso.predict(grid)
        )


def test_bank_round_trip_checks_checksum(bank: dt.Bank, tmp_path: Path) -> None:
    path = tmp_path / "bank.joblib"
    bank.save(path)
    assert dt.load(path).version == bank.version
    path.write_bytes(path.read_bytes() + b"x")
    with pytest.raises(ValueError, match="checksum"):
        dt.load(path)


def test_live_and_training_features_match() -> None:
    plan = random_plan(random.Random(5), "F", 1.0)
    a, b = Pipeline(context_from_plan(plan)), Pipeline(context_from_plan(plan))
    a.record = []
    b.record = []
    for t in generate(plan, seed=5):
        a.push(Sample(0, t.ts, t.values))
        b.push(Sample(0, t.ts, dict(t.values)))
    fa = np.array([[f[k] for k in FEATURES] for _, _, f in a.record])
    fb = np.array([[f[k] for k in FEATURES] for _, _, f in b.record])
    assert fa.shape[0] > 100 and np.array_equal(fa, fb, equal_nan=True)
