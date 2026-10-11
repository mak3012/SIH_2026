"""Per-wellbore processing of one sample at a time, without I/O:

    sample -> rig state -> derived channels -> history -> (every eval_s) features
           -> per hazard, gated by rig state: physics rule + detector LR
           -> prior from the roadmap at the current formation -> fusion -> alarm actions

The service (service.py) feeds it live samples; training (detectors.py) feeds it synthetic
ones, so live and training features come from the same code.
"""

from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from typing import Any, Protocol

import numpy as np

from api.realtime.channels import Sample
from api.realtime.derived import (
    DEFAULT_RHEOLOGY,
    DrillString,
    Rheology,
    Trajectory,
    annulus_for,
    dxc,
    ecd_sg,
    mse_mpa,
    torque_drag,
)
from api.realtime.features import GATES, HAZARDS, History, features, physics
from api.realtime.fusion import Action, AlarmManager, AlarmPolicy, window_prior
from api.realtime.rigstate import RigParams, detect

# Nominal casing IDs (in) for the common ODs; others: OD - 0.8 in.
CASING_ID = {20.0: 19.0, 13.375: 12.415, 10.75: 9.95, 9.625: 8.681, 7.0: 6.184, 5.5: 4.892}
KEY_FEATURES = {
    "kick": ("dflow_rel_1m", "pit_gain_5m", "pit_slope_5m", "pumps_age_min"),
    "loss": ("dflow_rel_1m", "pit_slope_5m", "pit_gain_10m", "pumps_age_min"),
    "pack_off": ("spp_rel_1m", "torque_rel_1m", "torque_cv_1m"),
    "washout": ("spp_slope_30m", "spp_rel_30m"),
    "stuck_pipe": ("hl_dev_rel_1m", "bit_speed_1m", "stick_min"),
    "tight_hole": ("hl_dev_rel_1m", "hl_dev_rel_max_1m"),
}
DRILLING = ("drilling_rotary", "drilling_slide")
# No trained detectors: uncalibrated LR = 10^(4 x physics score) when the rule fires, so a
# full-strength physics signal (score 1, LR 1e4) still reaches critical at typical priors.
FALLBACK_LOG10_LR = 4.0
# Hazards where a fired physics rule sets a minimum LR under the model's: tight hole, whose
# 1-3 min episodes are too short for the model (docs/realtime.md, Results).
PHYSICS_FLOOR: tuple[str, ...] = ("tight_hole",)


class Detectors(Protocol):
    version: str

    def lr(self, hazard: str, f: dict[str, float], phys: float) -> tuple[float, dict[str, Any]]: ...


@dataclass
class Context:
    """What the pipeline needs to know about the wellbore (context.py loads it)."""

    wellbore_id: int
    traj: Trajectory
    tops: list[tuple[str, float]] = field(default_factory=list)  # (formation, top md)
    shoes: list[tuple[float, float]] = field(default_factory=list)  # (shoe md, casing OD in)
    holes: list[tuple[float, float]] = field(default_factory=list)  # (from md, hole in)
    mw_sg: float = 1.2
    rheology: Rheology = DEFAULT_RHEOLOGY
    mu: float = 0.25
    mu_source: str = "default"
    string: DrillString = field(default_factory=DrillString)
    rig: RigParams = field(default_factory=RigParams)
    # (formation, hazard) -> (probability somewhere in the formation, source)
    priors: dict[tuple[str, str], tuple[float, str]] = field(default_factory=dict)
    roadmap_evidence: dict[tuple[str, str], list[dict[str, Any]]] = field(default_factory=dict)
    normal_sg: float = 1.03

    def formation_at(self, md: float) -> tuple[str | None, float | None]:
        """(formation, thickness m) at a depth; thickness None for the last formation."""
        name, thick = None, None
        for i, (f, top) in enumerate(self.tops):
            if top <= md:
                nxt = self.tops[i + 1][1] if i + 1 < len(self.tops) else None
                name, thick = f, (nxt - top if nxt is not None else None)
        return name, thick

    def hole_at(self, md: float) -> float:
        size = self.holes[0][1] if self.holes else 8.5
        for frm, h in self.holes:
            if frm <= md:
                size = h
        return size

    def shoe_above(self, md: float) -> tuple[float, float] | None:
        """(shoe md, casing ID in) of the deepest shoe above `md`."""
        above = [s for s in self.shoes if s[0] < md]
        if not above:
            return None
        md_s, od = max(above)
        return md_s, CASING_ID.get(round(od, 3), od - 0.8)


@dataclass
class Step:
    ts: datetime
    rig_state: str
    rule: str
    formation: str | None
    derived: dict[str, float]
    hazards: dict[str, dict[str, Any]] = field(default_factory=dict)
    actions: list[Action] = field(default_factory=list)
    gap_s: float | None = None


class Pipeline:
    def __init__(
        self,
        ctx: Context,
        detectors: Detectors | None = None,
        policy: AlarmPolicy | None = None,
        eval_s: float = 5.0,
        gap_s: float = 60.0,
        min_history_s: float = 120.0,
        physics_floor: tuple[str, ...] = PHYSICS_FLOOR,
    ) -> None:
        self.ctx, self.detectors, self.physics_floor = ctx, detectors, physics_floor
        self.alarms = AlarmManager(policy)
        self.eval_s, self.gap_s, self.min_history_s = eval_s, gap_s, min_history_s
        self.history = History()
        self.prev: tuple[float, float] | None = None  # (t, bit md)
        self.last_eval = -1e18
        self.stick_s = 0.0
        self.last_t: float | None = None
        self._td = lru_cache(maxsize=20_000)(self._td_raw)
        self._ecd = lru_cache(maxsize=20_000)(self._ecd_raw)
        self._tvd = lru_cache(maxsize=5_000)(lambda md5: ctx.traj.tvd(md5 * 5.0))
        # Training hook: (ts, rig state, features) of every evaluation.
        self.record: list[tuple[datetime, str, dict[str, float]]] | None = None

    # -- cached physics (rounded inputs: 5 m, 5 kN, 0.01 sg, 25 L/min)
    def _td_raw(self, md5: int, mw100: int, mode: str, wob5: int) -> tuple[float, float]:
        c = self.ctx
        hl, tq = torque_drag(c.traj, md5 * 5.0, mw100 / 100, c.mu, mode, c.string, wob5 * 5.0)
        return float(hl), float(tq)

    def _ecd_raw(self, md5: int, mw100: int, q25: int) -> tuple[float, float]:
        c, md = self.ctx, md5 * 5.0
        shoe = c.shoe_above(md)
        ann = annulus_for(
            md,
            c.hole_at(md),
            shoe[0] if shoe else None,
            shoe[1] if shoe else None,
            c.string.pipe_od_in,
            c.string.bha_od_in,
            c.string.bha_len_m,
        )
        return ecd_sg(mw100 / 100, q25 * 25.0, self._tvd(md5), ann, c.rheology)

    def set_context(self, ctx: Context) -> None:
        """New tops, roadmap or friction factor: swap the context and drop cached physics."""
        self.ctx = ctx
        self._td.cache_clear()
        self._ecd.cache_clear()
        self._tvd = lru_cache(maxsize=5_000)(lambda md5: ctx.traj.tvd(md5 * 5.0))

    def pause(self) -> None:
        """Data went stale: windows must not span the silence."""
        self.history.clear()
        self.prev = None

    def push(self, s: Sample) -> Step:
        c, v = self.ctx, s.values
        t = s.ts.timestamp()
        gap = None
        if self.last_t is not None and t - self.last_t > self.gap_s:
            gap = t - self.last_t
            self.pause()
        self.last_t = t
        bit = v.get("bit_md")
        vel = None
        if bit is not None and self.prev is not None and t > self.prev[0]:
            vel = (bit - self.prev[1]) / (t - self.prev[0])
        if bit is not None:
            self.prev = (t, bit)
        state, rule = detect(v, vel, c.rig)
        formation, thick = c.formation_at(bit) if bit is not None else (None, None)

        d: dict[str, float] = {}
        mw = v.get("mw_in") or c.mw_sg
        if "flow_in" in v and "flow_out" in v:
            d["delta_flow"] = v["flow_out"] - v["flow_in"]
        hl_pred = hl_free = None
        if bit is not None and bit > 0:
            md5, mw100 = round(bit / 5), round(mw * 100)
            d["tvd"] = self._tvd(md5)
            free, tq_free = self._td(md5, mw100, "rotating", 0)
            hl_free = free - c.string.block_kn
            mode = {"tripping_out": "pickup", "tripping_in": "slackoff"}.get(state, "rotating")
            wob = v.get("wob", 0.0) if state in DRILLING else 0.0
            if state == "connection":
                hl_pred = c.string.block_kn
            else:
                hl_pred, _ = self._td(md5, mw100, mode, round(wob / 5))
            d["hookload_pred"], d["torque_pred"] = hl_pred, tq_free
            if "hookload" in v:
                d["hookload_dev"] = v["hookload"] - hl_pred
            if v.get("flow_in", 0.0) > c.rig.flow_min_lpm:
                d["ecd"], d["annular_dp_bar"] = self._ecd(md5, mw100, round(v["flow_in"] / 25))
            hole = c.hole_at(bit)
            if state in DRILLING and v.get("rop", 0.0) > 0.5 and "wob" in v and "rpm" in v:
                if "torque" in v and v["rpm"] > 0:
                    d["mse"] = mse_mpa(v["wob"], v["rpm"], v["torque"], v["rop"], hole)
                x = dxc(v["rop"], max(v["rpm"], 1.0), v["wob"], hole, d.get("ecd", mw), c.normal_sg)
                if x is not None:
                    d["dxc"] = x
            shoe = c.shoe_above(bit)
            open_hole = shoe is None or bit > shoe[0]
            still = abs(vel or 0.0) < c.rig.move_min_m_s and v.get("rpm", 0.0) < c.rig.rpm_min
            dt = t - (self.history.t[-1] if self.history.t else t)
            self.stick_s = (
                self.stick_s + dt if (open_hole and still and state != "unknown") else 0.0
            )
            d["stick_s"] = self.stick_s
        self.history.add(t, {**v, "hl_pred": hl_pred, "hl_free": hl_free, "mse": d.get("mse")})
        step = Step(s.ts, state, rule, formation, d, gap_s=gap)
        if t - self.last_eval >= self.eval_s and self.history.span() >= self.min_history_s:
            self.last_eval = t
            self._evaluate(step, thick)
        return step

    def rolling_rop(self) -> float:
        r = np.array(self.history.s["rop"])
        r = r[np.isfinite(r) & (r > 0.5)]
        return float(r.mean()) if r.size else 15.0

    def _evaluate(self, step: Step, thick: float | None) -> None:
        c, pol = self.ctx, self.alarms.policy
        f = features(self.history, step.rig_state, self.stick_s)
        if self.record is not None:
            self.record.append((step.ts, step.rig_state, f))
        formation_h = thick / self.rolling_rop() if thick else pol.unknown_formation_h
        for h in HAZARDS:
            gated = step.rig_state in GATES[h]
            score, fired, lr = 0.0, False, 1.0
            detail: dict[str, Any] = {}
            if gated:
                score, fired = physics(h, f, step.rig_state)
                if self.detectors is not None:
                    lr, detail = self.detectors.lr(h, f, score)
                    if fired and h in self.physics_floor:
                        floor = 10 ** (FALLBACK_LOG10_LR * score)
                        if floor > lr:
                            lr, detail = floor, {**detail, "physics_floor": True}
                else:
                    lr = 10 ** (FALLBACK_LOG10_LR * score) if fired else 1.0
                    detail = {"calibrated": False, "method": "physics rule only"}
            p_f, src = c.priors.get((step.formation or "", h), (pol.default_p_formation, "default"))
            prior = window_prior(p_f, formation_h, pol)
            info = {
                "gated": gated,
                "physics": {"score": round(score, 3), "fired": fired},
                "lr": lr,
                "prior": prior,
                "detector": detail,
            }
            evidence = {
                "rig_state": step.rig_state,
                "rig_state_rule": step.rule,
                "formation": step.formation,
                "bit_md": None if self.prev is None else round(self.prev[1], 1),
                "features": {k: _r(f[k]) for k in KEY_FEATURES[h]},
                "physics": info["physics"],
                "detector": detail,
                "prior": {
                    "p_formation": p_f,
                    "source": src,
                    "formation_h": None if formation_h is None else round(formation_h, 2),
                    "episode_h": pol.episode_h,
                    "p_window": prior,
                },
                "roadmap_evidence": c.roadmap_evidence.get((step.formation or "", h), [])[:5],
            }
            step.hazards[h] = info
            step.actions += self.alarms.update(step.ts, h, prior, lr, evidence)


def _r(x: float) -> float | None:
    return None if x != x else round(float(x), 4)
