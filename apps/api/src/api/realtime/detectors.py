"""Hazard detectors: physics rule + IsolationForest + XGBoost on windowed features, calibrated
(isotonic) into likelihood ratios. Training: group K-fold by wellbore, never random rows.

Per hazard, on the evaluations whose rig state passes the hazard's gate:

1. IsolationForest fitted on normal windows (label 0) -> anomaly score (higher = stranger).
2. XGBoost on [windowed features, physics score, anomaly score] -> p_raw.
3. Out-of-fold p_raw from GroupKFold(groups = wellbore): every well is scored by models that
   never saw it. Isotonic regression maps out-of-fold p_raw to a calibrated p.
4. Likelihood ratio: LR = [p / (1 - p)] / [pi / (1 - pi)], pi = training base rate in the gated
   windows. The LR is base-rate free; fusion multiplies it into the roadmap prior.

p is clipped to [1/(n+2), 1 - 1/(n+2)] and the LR to [1e-3, 1e4]: a finite sample cannot
justify certainty.

    python -m api.realtime train --wells 60 --test-wells 12  (make rt-models)
"""

import hashlib
import random
import statistics
from collections.abc import Iterable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import joblib
import numpy as np
from sklearn.ensemble import IsolationForest
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score
from sklearn.model_selection import GroupKFold
from xgboost import XGBClassifier

from api.realtime.channels import Sample
from api.realtime.context import context_from_plan
from api.realtime.features import FEATURES, GATES, HAZARDS
from api.realtime.fusion import AlarmPolicy
from api.realtime.pipeline import Detectors, Pipeline
from api.realtime.synthetic import Plan, generate, random_plan

VERSION = "detectors@1.0.0"
COLUMNS = [*FEATURES, "physics_score"]
LR_MIN, LR_MAX = 1e-3, 1e4
DEFAULT_PATH = Path("data/models/realtime/detectors.joblib")


class FastIForest:
    """IsolationForest.score_samples for a few rows at a time, without joblib's per-tree
    overhead (21 ms -> about 0.3 ms per row live). Same arithmetic as scikit-learn: float32
    inputs, tree walk, decision-path length + average path length of the leaf - 1, normalised
    by c(max_samples). test_fast_iforest_matches_sklearn checks equality."""

    def __init__(self, f: IsolationForest) -> None:
        from sklearn.ensemble._iforest import _average_path_length

        self.trees = [
            (t.tree_.children_left.tolist(), t.tree_.children_right.tolist(),
             t.tree_.feature.tolist(), t.tree_.threshold.tolist(), np.asarray(feats),
             np.asarray(dpl, dtype=float).tolist(), np.asarray(apl, dtype=float).tolist())
            for t, feats, dpl, apl in zip(f.estimators_, f.estimators_features_,
                                          f._decision_path_lengths,
                                          f._average_path_length_per_tree, strict=True)
        ]  # fmt: skip
        self.den = len(f.estimators_) * float(_average_path_length([f._max_samples])[0])

    def score_samples(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.float32)
        out = np.empty(len(x))
        for i, row in enumerate(x):
            depth = 0.0
            for left, right, feat, thr, feats, dpl, apl in self.trees:
                v = row[feats].tolist()
                node = 0
                while left[node] != -1:
                    node = left[node] if v[feat[node]] <= thr[node] else right[node]
                depth += dpl[node] + apl[node] - 1.0
            out[i] = -(2 ** (-depth / self.den)) if self.den else -1.0
        return out


@dataclass
class HazardModel:
    hazard: str
    states: tuple[str, ...]
    iforest: IsolationForest
    xgb: XGBClassifier
    iso: IsotonicRegression
    base_rate: float
    p_clip: tuple[float, float]
    metrics: dict[str, float] = field(default_factory=dict)

    def lr_batch(self, x: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """(LR, calibrated p, anomaly) for rows of COLUMNS."""
        fast = self.__dict__.get("_fast")
        if fast is None:
            fast = self.__dict__["_fast"] = (FastIForest(self.iforest), self.xgb.get_booster())
        anomaly = -fast[0].score_samples(np.nan_to_num(x))
        p_raw = np.asarray(fast[1].inplace_predict(np.column_stack([x, anomaly])), dtype=float)
        # = self.iso.predict (linear interpolation, clipped at the ends), without its overhead
        p = np.clip(np.interp(p_raw, self.iso.X_thresholds_, self.iso.y_thresholds_), *self.p_clip)
        pi = self.base_rate
        lr = np.clip((p / (1 - p)) / (pi / (1 - pi)), LR_MIN, LR_MAX)
        return lr, p, anomaly


@dataclass
class Bank:
    version: str
    models: dict[str, HazardModel]
    trained: dict[str, Any]
    sha256: str = ""  # of the file it was loaded from (set by load; recorded in the audit log)

    def lr(self, hazard: str, f: dict[str, float], phys: float) -> tuple[float, dict[str, Any]]:
        m = self.models.get(hazard)
        if m is None:
            return 1.0, {"calibrated": False, "method": "no model for hazard"}
        x = np.array([[*(f[k] for k in FEATURES), phys]], dtype=float)
        lr, p, anomaly = m.lr_batch(x)
        return float(lr[0]), {
            "calibrated": True,
            "method": "xgboost+isolation_forest, isotonic",
            "version": self.version,
            "p_calibrated": round(float(p[0]), 4),
            "anomaly": round(float(anomaly[0]), 4),
            "base_rate": round(m.base_rate, 4),
        }

    def save(self, path: Path) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(self, path)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        path.with_suffix(".sha256").write_text(digest + "\n")
        return digest


def load(path: Path) -> Bank:
    """Load a bank written by `train`. joblib unpickles code: only load files this project
    trained, and the checksum next to it must match."""
    want = path.with_suffix(".sha256").read_text().strip()
    if hashlib.sha256(path.read_bytes()).hexdigest() != want:
        raise ValueError(f"{path}: checksum mismatch")
    bank = joblib.load(path)
    if not isinstance(bank, Bank):
        raise TypeError(f"{path} is not a detector bank")
    bank.sha256 = want
    return bank


# ------------------------------------------------------------------ dataset


def well_rows(args: tuple[int, float]) -> list[tuple[int, str, list[float], list[str]]]:
    """Evaluations of one synthetic well: (well seed, rig state, COLUMNS values, labels)."""
    seed, hours = args
    from api.realtime.features import physics

    # Every hazard scheduled in a third of the wells, whatever the random rates do.
    scheduled = [HAZARDS[seed % len(HAZARDS)], HAZARDS[(seed + 3) % len(HAZARDS)]]
    plan = random_plan(random.Random(seed), f"T{seed}", hours, scheduled)
    pipe = Pipeline(context_from_plan(plan))
    pipe.record = []
    rows = []
    for tick in generate(plan, seed=seed):
        pipe.push(Sample(0, tick.ts, tick.values))
        if pipe.record:
            ts, state, f = pipe.record.pop()
            phys = max((physics(h, f, state)[0] for h in HAZARDS if state in GATES[h]), default=0.0)
            rows.append((seed, state, [*(f[k] for k in FEATURES), phys], sorted(tick.labels)))
    return rows


def dataset(seeds: Iterable[int], hours: float, workers: int | None = None) -> list[Any]:
    with ProcessPoolExecutor(workers) as ex:
        return [r for rows in ex.map(well_rows, [(s, hours) for s in seeds]) for r in rows]


# ------------------------------------------------------------------ training


def _physics_column(hazard: str, x: np.ndarray, states: list[str]) -> np.ndarray:
    """Recompute the physics score per hazard (the dataset stores the max over hazards)."""
    from api.realtime.features import physics

    out = np.empty(len(x))
    for i, (row, st) in enumerate(zip(x, states, strict=True)):
        out[i] = physics(hazard, dict(zip(FEATURES, row[: len(FEATURES)], strict=True)), st)[0]
    return out


def _fit(x: np.ndarray, y: np.ndarray, seed: int) -> tuple[IsolationForest, XGBClassifier]:
    iforest = IsolationForest(n_estimators=50, random_state=seed).fit(np.nan_to_num(x[y == 0]))
    anomaly = -iforest.score_samples(np.nan_to_num(x))
    xgb = XGBClassifier(
        n_estimators=200,
        max_depth=4,
        learning_rate=0.1,
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=seed,
        n_jobs=4,
        eval_metric="logloss",
    ).fit(np.column_stack([x, anomaly]), y)
    return iforest, xgb


def train_hazard(rows: list[Any], hazard: str, folds: int = 5, seed: int = 0) -> HazardModel:
    gate = GATES[hazard]
    sel = [r for r in rows if r[1] in gate]
    groups = np.array([r[0] for r in sel])
    states = [r[1] for r in sel]
    x = np.array([r[2] for r in sel], dtype=float)
    x[:, -1] = _physics_column(hazard, x, states)
    y = np.array([hazard in r[3] for r in sel], dtype=int)
    if y.sum() < 10 or len(set(groups[y == 1])) < folds:
        raise ValueError(f"{hazard}: too few positive windows/wells ({y.sum()})")
    oof = np.zeros(len(y))
    for tr, te in GroupKFold(n_splits=folds).split(x, y, groups):
        assert not set(groups[tr]) & set(groups[te])  # no well on both sides
        iforest, xgb = _fit(x[tr], y[tr], seed)
        anomaly = -iforest.score_samples(np.nan_to_num(x[te]))
        oof[te] = xgb.predict_proba(np.column_stack([x[te], anomaly]))[:, 1]
    iso = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip").fit(oof, y)
    iforest, xgb = _fit(x, y, seed)
    n = len(y)
    cal = iso.predict(oof)
    metrics = {
        "windows": n,
        "positives": int(y.sum()),
        "wells": len(set(groups)),
        "base_rate": float(y.mean()),
        "auc_oof": float(roc_auc_score(y, oof)),
        "pr_auc_oof": float(average_precision_score(y, oof)),
        "auc_physics": float(roc_auc_score(y, x[:, -1])),
        "brier_raw": float(brier_score_loss(y, oof)),
        "brier_calibrated": float(brier_score_loss(y, cal)),
    }
    return HazardModel(
        hazard, gate, iforest, xgb, iso, float(y.mean()), (1 / (n + 2), 1 - 1 / (n + 2)), metrics
    )


def train(rows: list[Any], folds: int = 5, seed: int = 0) -> Bank:
    models = {h: train_hazard(rows, h, folds, seed) for h in HAZARDS}
    return Bank(VERSION, models, {"rows": len(rows), "folds": folds, "seed": seed})


# ------------------------------------------------------------------ event-level evaluation


def _episodes(ticks: list[tuple[datetime, frozenset[str]]], hazard: str) -> list[list[datetime]]:
    eps: list[list[datetime]] = []
    inside = False
    for ts, labels in ticks:
        if hazard in labels and not inside:
            eps.append([ts, ts])
        if hazard in labels:
            eps[-1][1] = ts
        inside = hazard in labels
    return eps


def replay_well(args: tuple[int, float, Detectors | None]) -> dict[str, Any]:
    """Run one held-out synthetic well through the full pipeline (fusion included)."""
    seed, hours, bank = args
    plan: Plan = random_plan(random.Random(seed), f"E{seed}", hours)
    pipe = Pipeline(context_from_plan(plan), bank, AlarmPolicy())
    ticks, alerts = [], []
    for tick in generate(plan, seed=seed):
        ticks.append((tick.ts, tick.labels))
        step = pipe.push(Sample(0, tick.ts, tick.values))
        for a in step.actions:
            if a.kind in ("raise", "reopen", "escalate"):
                alerts.append((a.hazard, a.ts, a.tier, a.kind))
    span_h = (ticks[-1][0] - ticks[0][0]).total_seconds() / 3600
    return {
        "seed": seed,
        "hours": span_h,
        "alerts": alerts,
        "episodes": {h: _episodes(ticks, h) for h in HAZARDS},
    }


def event_metrics(results: list[dict[str, Any]], grace_s: float = 300) -> dict[str, Any]:
    """Per hazard: episodes, detected at advisory+ and warning+, median delay from onset,
    false alerts per 12 h (raised outside any episode of that hazard +- grace)."""
    hours = sum(r["hours"] for r in results)
    out = {}
    for h in HAZARDS:
        n = det1 = det2 = false = 0
        delays: list[float] = []
        for r in results:
            eps = r["episodes"][h]
            mine = [a for a in r["alerts"] if a[0] == h]
            for start, end in eps:
                n += 1
                hit = [
                    a
                    for a in mine
                    if start.timestamp() - grace_s <= a[1].timestamp() <= end.timestamp() + grace_s
                ]
                if hit:
                    det1 += 1
                    delays.append(max(0.0, (min(a[1] for a in hit) - start).total_seconds()))
                if any(a[2] >= 2 for a in hit):
                    det2 += 1
            for a in mine:
                if a[3] == "escalate":
                    continue
                if not any(
                    s.timestamp() - grace_s <= a[1].timestamp() <= e.timestamp() + grace_s
                    for s, e in eps
                ):
                    false += 1
        out[h] = {
            "episodes": n,
            "detected_advisory": det1,
            "detected_warning": det2,
            "median_delay_min": round(statistics.median(delays) / 60, 1) if delays else None,
            "false_alerts_per_12h": round(false / hours * 12, 2) if hours else None,
        }
    out["_hours"] = round(hours, 1)
    return out


def evaluate(bank: Detectors | None, seeds: Iterable[int], hours: float) -> dict[str, Any]:
    with ProcessPoolExecutor() as ex:
        res = list(ex.map(replay_well, [(s, hours, bank) for s in seeds]))
    return event_metrics(res)


def report(bank: Bank, events: dict[str, Any], baseline: dict[str, Any]) -> str:
    """Markdown tables for docs/realtime.md."""
    lines = [
        f"Model `{bank.version}`, {bank.trained['rows']:,} training evaluations, "
        f"{bank.trained['folds']}-fold group K-fold by well.",
        "",
        "| Hazard | Wells | Windows | Positives | AUC (physics only) | AUC (out-of-fold) "
        "| PR-AUC | Brier raw → calibrated |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for h, m in bank.models.items():
        k = m.metrics
        lines.append(
            f"| {h} | {k['wells']} | {k['windows']:,} | {k['positives']:,} | "
            f"{k['auc_physics']:.3f} | {k['auc_oof']:.3f} | {k['pr_auc_oof']:.3f} | "
            f"{k['brier_raw']:.4f} → {k['brier_calibrated']:.4f} |"
        )
    lines += [
        "",
        f"Event level on held-out wells ({events['_hours']} h of data, full pipeline and alarm "
        "policy, default prior; physics-only baseline in brackets):",
        "",
        "| Hazard | Episodes | Detected (advisory+) | Detected (warning+) | Median delay (min) "
        "| False alerts / 12 h |",
        "|---|---|---|---|---|---|",
    ]
    for h in HAZARDS:
        e, b = events[h], baseline[h]
        lines.append(
            f"| {h} | {e['episodes']} | {e['detected_advisory']} [{b['detected_advisory']}] | "
            f"{e['detected_warning']} [{b['detected_warning']}] | {e['median_delay_min']} "
            f"[{b['median_delay_min']}] | {e['false_alerts_per_12h']} "
            f"[{b['false_alerts_per_12h']}] |"
        )
    return "\n".join(lines)
