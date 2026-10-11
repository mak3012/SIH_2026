"""Windowed features over the recent history, and the physics rule per hazard.

One feature vector per evaluation (every few seconds of data time). Windows: 1, 5, 10 and 30
minutes back from the newest sample. Missing values are NaN (XGBoost handles them; the
IsolationForest sees 0). The same code builds training rows and live rows.
"""

from math import isfinite

import numpy as np

from api.realtime.rigstate import STATES

HORIZON_S = 1800.0
SERIES = (
    "flow_in",
    "flow_out",
    "pit_vol",
    "spp",
    "torque",
    "rpm",
    "hookload",
    "hl_pred",
    "hl_free",
    "bit_md",
    "rop",
    "mse",
)
FEATURES = [
    "dflow_rel_1m",
    "dflow_rel_5m",
    "dflow_slope_5m",
    "pit_gain_5m",
    "pit_gain_10m",
    "pit_slope_5m",
    "spp_rel_1m",
    "spp_rel_30m",
    "spp_slope_30m",
    "torque_rel_1m",
    "torque_cv_1m",
    "hl_dev_rel_1m",
    "hl_dev_rel_max_1m",
    "bit_speed_1m",
    "stick_min",
    "rop_rel_1m",
    "mse_rel_1m",
    "flow_change_1m",
    "pumps_age_min",
    *[f"state_{s}" for s in STATES],
]

# Rig states in which each detector runs (gating). Outside them the hazard is not evaluated
# and its probability falls back to the prior.
GATES: dict[str, tuple[str, ...]] = {
    "kick": ("drilling_rotary", "drilling_slide", "circulating", "reaming"),
    "loss": ("drilling_rotary", "drilling_slide", "circulating", "reaming"),
    "pack_off": ("drilling_rotary", "drilling_slide", "circulating", "reaming"),
    "washout": ("drilling_rotary", "drilling_slide", "circulating"),
    "stuck_pipe": (
        "tripping_out",
        "tripping_in",
        "connection",
        "static",
        "reaming",
        "circulating",
    ),
    "tight_hole": ("tripping_out", "tripping_in", "reaming"),
}
HAZARDS = tuple(GATES)


class History:
    """Recent samples per series, trimmed to HORIZON_S of data time."""

    def __init__(self) -> None:
        self.t: list[float] = []
        self.s: dict[str, list[float]] = {k: [] for k in SERIES}

    def add(self, t: float, row: dict[str, float | None]) -> None:
        self.t.append(t)
        for k in SERIES:
            v = row.get(k)
            self.s[k].append(float(v) if v is not None and isfinite(v) else np.nan)
        if len(self.t) > 64 and self.t[0] < t - HORIZON_S - 60:
            cut = int(np.searchsorted(np.array(self.t), t - HORIZON_S))
            self.t = self.t[cut:]
            for k in SERIES:
                self.s[k] = self.s[k][cut:]

    def clear(self) -> None:
        self.__init__()  # type: ignore[misc]

    def span(self) -> float:
        return self.t[-1] - self.t[0] if self.t else 0.0


def _nanmean(a: np.ndarray) -> float:
    a = a[np.isfinite(a)]
    return float(a.mean()) if a.size else np.nan


def _nanmedian(a: np.ndarray) -> float:
    a = a[np.isfinite(a)]
    return float(np.median(a)) if a.size else np.nan


def _rel(a: float, b: float) -> float:
    """a / b - 1, NaN when b is missing or ~0 (e.g. ROP off bottom)."""
    return a / b - 1 if np.isfinite(a) and np.isfinite(b) and abs(b) > 1e-9 else np.nan


def _slope(t: np.ndarray, y: np.ndarray) -> float:
    """Least-squares slope dy/dt (per second); NaN with fewer than 5 points."""
    ok = np.isfinite(y)
    if ok.sum() < 5:
        return np.nan
    t, y = t[ok], y[ok]
    t = t - t.mean()
    d = float((t * t).sum())
    return float((t * (y - y.mean())).sum() / d) if d > 0 else np.nan


def _change(y: np.ndarray) -> float:
    ok = np.flatnonzero(np.isfinite(y))
    return float(y[ok[-1]] - y[ok[0]]) if ok.size >= 2 else np.nan


def features(h: History, state: str, stick_s: float) -> dict[str, float]:
    t = np.array(h.t)
    now = t[-1]
    s = {k: np.array(v) for k, v in h.s.items()}
    w1, w5, w10 = t >= now - 60, t >= now - 300, t >= now - 600
    q_in, q_out = s["flow_in"], s["flow_out"]
    # Flow difference at 1 L/min resolution: nothing below what a flow meter can report may
    # carry signal (a model once learned 1e-17 "flow" with the pumps off).
    dq = np.round(q_out - q_in)
    q1 = max(_nanmean(q_in[w1]), 100.0) if np.isfinite(_nanmean(q_in[w1])) else 100.0
    q5 = max(_nanmean(q_in[w5]), 100.0) if np.isfinite(_nanmean(q_in[w5])) else 100.0
    pumps = q_in > 100
    spn = np.where(pumps, s["spp"] / (np.maximum(q_in, 1.0) / 2200) ** 1.8, np.nan)
    rot = s["rpm"] > 5
    tq = np.where(rot, s["torque"], np.nan)
    hl_dev = (s["hookload"] - s["hl_pred"]) / np.maximum(s["hl_free"], 50.0)
    bit_v = np.abs(np.diff(s["bit_md"][w1])) / np.maximum(np.diff(t[w1]), 1e-6)
    # Pumps age: minutes since flow-in last changed by more than 10 %.
    q_ok = np.where(np.isfinite(q_in), q_in, 0.0)
    jumps = np.flatnonzero(np.abs(np.diff(q_ok)) > 0.1 * np.maximum(q_ok[1:], 100.0))
    pumps_age = (now - t[jumps[-1] + 1]) / 60 if jumps.size else h.span() / 60
    spp30_med = _nanmedian(spn)
    f = {
        "dflow_rel_1m": _nanmean(dq[w1]) / q1,
        "dflow_rel_5m": _nanmean(dq[w5]) / q5,
        "dflow_slope_5m": _slope(t[w5], dq[w5] / q5) * 60,
        "pit_gain_5m": _change(s["pit_vol"][w5]),
        "pit_gain_10m": _change(s["pit_vol"][w10]),
        "pit_slope_5m": _slope(t[w5], s["pit_vol"][w5]) * 3600,
        "spp_rel_1m": _rel(_nanmean(spn[w1]), _nanmedian(spn[w10])),
        "spp_rel_30m": _rel(_nanmean(spn[w1]), spp30_med),
        "spp_slope_30m": _slope(t, spn) * 3600 / spp30_med if abs(spp30_med) > 1e-9 else np.nan,
        "torque_rel_1m": _rel(_nanmean(tq[w1]), _nanmedian(tq[w10])),
        "torque_cv_1m": _rel(float(np.nanstd(tq[w1])), _nanmean(tq[w1])) + 1
        if np.isfinite(tq[w1]).sum() >= 3
        else np.nan,
        "hl_dev_rel_1m": _nanmean(hl_dev[w1]),
        "hl_dev_rel_max_1m": float(np.nanmax(hl_dev[w1]))
        if np.isfinite(hl_dev[w1]).any()
        else np.nan,
        "bit_speed_1m": float(np.nanmean(bit_v))
        if bit_v.size and np.isfinite(bit_v).any()
        else np.nan,
        "stick_min": stick_s / 60,
        "rop_rel_1m": _rel(_nanmean(s["rop"][w1]), _nanmedian(s["rop"][w10])),
        "mse_rel_1m": _rel(_nanmean(s["mse"][w1]), _nanmedian(s["mse"][w10])),
        "flow_change_1m": abs(_change(q_in[w1])) / q1 if np.isfinite(_change(q_in[w1])) else np.nan,
        "pumps_age_min": min(pumps_age, 30.0),
    }
    for st in STATES:
        f[f"state_{st}"] = 1.0 if st == state else 0.0
    return {k: (v if isfinite(v) else np.nan) for k, v in f.items()}


def _clip(x: float) -> float:
    return 0.0 if not isfinite(x) else min(max(x, 0.0), 1.0)


# Physics rule per hazard: (score in [0, 1], fired). Thresholds in docs/realtime.md.
def physics(hazard: str, f: dict[str, float], state: str) -> tuple[float, bool]:
    settled = f["pumps_age_min"] > 2  # return flow lags a pump-rate change by ~1-2 min
    if hazard == "kick":
        score = _clip(f["dflow_rel_1m"] / 0.10) * _clip(f["pit_gain_5m"] / 1.0)
        fired = settled and f["dflow_rel_1m"] > 0.05 and f["pit_gain_5m"] > 0.5
    elif hazard == "loss":
        score = _clip(-f["dflow_rel_1m"] / 0.3) * _clip(-f["pit_slope_5m"] / 5.0)
        fired = settled and f["dflow_rel_1m"] < -0.10 and f["pit_slope_5m"] < -1.0
    elif hazard == "pack_off":
        tq = max(_clip(f["torque_rel_1m"] / 0.3), _clip(f["torque_cv_1m"] / 0.2))
        score = _clip(f["spp_rel_1m"] / 0.25) * tq
        fired = f["spp_rel_1m"] > 0.10 and (f["torque_rel_1m"] > 0.15 or f["torque_cv_1m"] > 0.12)
    elif hazard == "washout":
        score = _clip(-f["spp_slope_30m"] / 0.15) * _clip(-f["spp_rel_30m"] / 0.08)
        fired = settled and f["spp_slope_30m"] < -0.05 and f["spp_rel_30m"] < -0.04
    elif hazard == "stuck_pipe":
        still = isfinite(f["bit_speed_1m"]) and f["bit_speed_1m"] < 0.02
        over = _clip(f["hl_dev_rel_1m"] / 0.4) if still else 0.0
        score = max(over, _clip((f["stick_min"] - 5) / 25))
        fired = (still and f["hl_dev_rel_1m"] > 0.25) or f["stick_min"] > 20
    elif hazard == "tight_hole":
        dev = f["hl_dev_rel_1m"] if state != "tripping_in" else -f["hl_dev_rel_1m"]
        score = _clip(dev / 0.2)
        fired = dev > 0.08
    else:
        raise ValueError(hazard)
    return score, bool(fired)
