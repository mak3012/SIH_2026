"""Synthetic real-time drilling data with labelled hazard episodes.

Not a simulator of rock or hydraulics in detail: a rig-floor sequence (drill a stand,
connection, wiper trip) whose channels follow the same physics as derived.py (soft-string
hookload and torque, pump pressure ~ Q^1.8, lagged return flow, pit volume as the integral of
flow out minus flow in) plus noise, and the nuisances that fool naive rules: pump-rate changes
(return flow lags), connection flowback, pit transfers, sensor spikes and dropouts.

Hazard episodes and their signatures (labels = the hazard while the episode lasts):

    kick        return flow rises 5-25 % over minutes, pit gains, SPP falls slightly
    loss        return flow falls 10-90 %, pit falls, SPP falls slightly
    pack_off    SPP rises 10-40 %, torque rises and turns erratic, return flow dips
    washout     SPP declines 8-20 % over 30-90 min at constant flow
    stuck_pipe  after a long static connection: overpull far above pickup, pipe does not move
    tight_hole  10-30 % overpull over a 20-60 m interval while tripping out

`plan_from_db` turns a seeded wellbore (survey, tops, casing, events) into a Plan, so the
WITSML simulator replays the synthetic Upper Assam wells with their recorded events.
"""

import math
import random
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any

from api.realtime.derived import DrillString, Trajectory, torque_drag

HAZARDS = ("kick", "loss", "pack_off", "washout", "stuck_pipe", "tight_hole")
STAND_M = 28.0
# Reporting resolution per channel (typical rig sensors).
RESOLUTION = {"bit_md": 0.01, "hole_md": 0.01, "hookload": 0.1, "wob": 0.1, "rpm": 0.1,
              "torque": 0.01, "spp": 0.1, "flow_in": 1.0, "flow_out": 1.0, "pit_vol": 0.01,
              "mw_in": 0.001, "rop": 0.01}  # fmt: skip
SPP_AT_2200 = 190.0  # bar at 2200 L/min, scaled by Q^1.8


@dataclass
class Plan:
    name: str
    survey: list[tuple[float, float, float]]  # md, inc deg, azi deg
    tops: list[tuple[str, float]]  # formation, md
    shoes: list[tuple[float, float]]  # shoe md, casing OD in
    events: list[tuple[str, float]]  # hazard, md: happens when the bit gets there
    start_md: float
    end_md: float
    mw_sg: float = 1.2
    rop_m_h: float = 18.0
    hole_in: float = 8.5
    mu: float = 0.25  # true friction factor (unknown to the detectors)
    rates: dict[str, float] = field(default_factory=dict)  # random episodes per stand
    wiper_every: int = 8  # stands between wiper trips (0 = none)
    wiper_tight: float = 0.6  # chance a routine wiper trip meets tight hole (0: only scheduled)


@dataclass
class Tick:
    ts: datetime
    values: dict[str, float]
    labels: frozenset[str]


def random_plan(
    rng: random.Random, name: str, hours: float = 12.0, scheduled: list[str] | None = None
) -> Plan:
    """A training well: random trajectory, depth, rates, friction and a high hazard rate (the
    detectors need positives; their output is a likelihood ratio, so the training base rate
    does not leak into the alarm probability)."""
    kop, inc = rng.uniform(300, 900), rng.choice([0.0, rng.uniform(10, 45)])
    survey = [(0.0, 0.0, 0.0), (kop, 0.0, 0.0), (kop + 600, inc, 120.0), (5000.0, inc, 120.0)]
    rop = rng.uniform(10, 30)
    start = rng.uniform(1200, 2600)
    drilled = rop * hours * 0.6
    # Scheduled hazards on top of the random rates, so every hazard type appears in many wells
    # (group K-fold needs positives in every fold).
    events = [
        (h, start + rng.uniform(0.1, 0.8) * drilled)
        for h in (scheduled if scheduled is not None else rng.sample(HAZARDS, 2))
    ]
    return Plan(
        name=name,
        survey=survey,
        tops=[("A", 0.0), ("B", start + 100), ("C", start + 300)],
        shoes=[(start - rng.uniform(200, 800), 9.625)],
        events=sorted(events, key=lambda e: e[1]),
        start_md=start,
        end_md=start + drilled,
        mw_sg=rng.uniform(1.08, 1.45),
        rop_m_h=rop,
        hole_in=rng.choice([8.5, 12.25]),
        mu=rng.uniform(0.18, 0.35),
        rates={"kick": 0.1, "loss": 0.1, "pack_off": 0.1, "washout": 0.08, "stuck_pipe": 0.1},
        wiper_every=rng.choice([4, 6]),
    )


@lru_cache(maxsize=50_000)
def _td(
    traj_key: int, md5: int, mw100: int, mu100: int, mode: str, wob5: int
) -> tuple[float, float]:
    return torque_drag(_TRAJ[traj_key], md5 * 5.0, mw100 / 100, mu100 / 100, mode, wob_kn=wob5 * 5)


_TRAJ: dict[int, Trajectory] = {}


class _Rig:
    """Integrates the rig state at a fixed step and emits Ticks."""

    def __init__(self, plan: Plan, rng: random.Random, start: datetime, dt: float) -> None:
        self.p, self.rng, self.dt = plan, rng, dt
        self.t = start
        self.bit = self.hole = plan.start_md
        self.q_cmd, self.q_in, self.q_out = 0.0, 0.0, 0.0
        self.pit = rng.uniform(60, 90)
        self.spp_scale = rng.uniform(0.85, 1.15)
        self.rop = plan.rop_m_h
        self.key = id(plan)
        _TRAJ[self.key] = Trajectory.from_stations(plan.survey)
        self.effects: list[dict[str, Any]] = []
        self.q_nominal = rng.uniform(1800, 2600) * (1.0 if plan.hole_in < 10 else 1.4)

    # -- effects: dicts with labels and multipliers/additions, active while their clock runs
    def effect(self, label: str | None, seconds: float, **kw: float) -> dict[str, Any]:
        e = {"label": label, "left": seconds, "age": 0.0, "dur": seconds, **kw}
        self.effects.append(e)
        return e

    def ramp(self, e: dict[str, Any], tau: float = 180.0) -> float:
        return 1 - math.exp(-e["age"] / tau)

    def step(self, op: str, *, v: float = 0.0, wob: float = 0.0, rpm: float = 0.0) -> Tick:
        p, rng, dt = self.p, self.rng, self.dt
        pumps = op in ("drill", "slide", "circ", "ream")
        if op in ("drill", "slide"):
            self.bit = max(self.bit, self.hole)  # drilling starts on bottom
        self.q_cmd = self.q_nominal if pumps else 0.0
        # Pumps ramp over ~30 s; returns lag the pumps (fill-up on, flowback off).
        self.q_in += (self.q_cmd - self.q_in) * min(1.0, dt / 15)
        tau = 25.0 if self.q_in > self.q_out else 60.0
        self.q_out += (self.q_in - self.q_out) * min(1.0, dt / tau)
        if op == "shut_in":  # BOP closed: no returns at surface
            self.q_out = 0.0
        labels: set[str] = set()
        q_out_mul, q_out_add, spp_mul, tq_mul, tq_noise, overpull = 1.0, 0.0, 1.0, 1.0, 0.0, 0.0
        stuck = False
        for e in self.effects:
            e["age"] += dt
            e["left"] -= dt
            if e["label"]:
                labels.add(e["label"])
            r = self.ramp(e, e.get("tau", 180.0))
            q_out_mul *= 1 + e.get("q_out", 0.0) * r
            q_out_add += e.get("q_out_abs", 0.0) * r
            spp_mul *= 1 + e.get("spp", 0.0) * r
            tq_mul *= 1 + e.get("torque", 0.0) * r
            tq_noise += e.get("torque_noise", 0.0) * r
            overpull += e.get("overpull", 0.0) * r
            stuck |= bool(e.get("stuck"))
        self.effects = [e for e in self.effects if e["left"] > 0]
        if stuck:
            v = 0.0
        # Depth
        if op in ("drill", "slide"):
            self.rop = max(
                2.0, self.rop + rng.gauss(0, 0.3) * dt / 5 + (p.rop_m_h - self.rop) * 0.002
            )
            self.bit += self.rop * dt / 3600
            self.hole = max(self.hole, self.bit)
        else:
            self.bit = min(self.hole, max(0.0, self.bit + v * dt))
        # Flows and pit: hole growth takes mud from the pits.
        q_out = max(0.0, self.q_out * q_out_mul + q_out_add)
        area = math.pi * (p.hole_in * 0.0254) ** 2 / 4
        growth = area * (self.rop * dt / 3600 if op in ("drill", "slide") else 0.0)
        self.pit += (q_out - self.q_in) / 60000 * dt - growth
        if rng.random() < dt / 7200:  # pit transfer every ~2 h
            self.effect(None, 120, pit_step=rng.choice([-1, 1]) * rng.uniform(2, 5) / 120)
        self.pit += sum(e.get("pit_step", 0.0) for e in self.effects) * dt
        # Mechanics
        mode = {"trip_out": "pickup", "trip_in": "slackoff"}.get(op, "rotating")
        w = wob if op in ("drill", "slide") else 0.0
        hl, tq = _td(
            self.key,
            round(self.bit / 5),
            round(p.mw_sg * 100),
            round(p.mu * 100),
            mode,
            round(w / 5),
        )
        if op == "slips":
            hl, tq = DrillString().block_kn, 0.0
        elif op in ("static",) and not stuck:
            hl, _ = _td(
                self.key,
                round(self.bit / 5),
                round(p.mw_sg * 100),
                round(p.mu * 100),
                "rotating",
                0,
            )
        hl += overpull * (hl - DrillString().block_kn)
        rot = rpm > 0
        # Off-bottom torque in a vertical hole is not zero (contact, vibration): 1.5 kN.m + depth.
        base = 1.5 + 0.002 * self.bit
        torque = (tq + base + (0.03 * w if op == "drill" else 0.0)) * tq_mul if rot else 0.0
        if rot:
            torque *= 1 + rng.gauss(0, 0.04 + tq_noise)
        spp = SPP_AT_2200 * self.spp_scale * (self.q_in / 2200) ** 1.8 * spp_mul
        vals = {
            "bit_md": self.bit,
            "hole_md": self.hole,
            "hookload": float(hl) * (1 + rng.gauss(0, 0.01)),
            "wob": max(0.0, w + rng.gauss(0, 2)) if w else abs(rng.gauss(0, 1)),
            "rpm": max(0.0, rpm + rng.gauss(0, 1.5)) if rot else 0.0,
            "torque": max(0.0, torque),
            "spp": max(0.0, spp * (1 + rng.gauss(0, 0.01))),
            "flow_in": max(0.0, self.q_in * (1 + rng.gauss(0, 0.005))),
            "flow_out": max(0.0, q_out * (1 + rng.gauss(0, 0.02))),
            "pit_vol": self.pit + rng.gauss(0, 0.05),
            "mw_in": p.mw_sg + rng.gauss(0, 0.003),
            "rop": self.rop if op in ("drill", "slide") else 0.0,
        }
        # Sensor faults: a spike or a dropped channel now and then.
        if rng.random() < 0.002:
            k = rng.choice(list(vals))
            vals[k] *= rng.uniform(0.3, 2.0)
        if rng.random() < 0.002:
            vals.pop(rng.choice(["flow_out", "pit_vol", "torque", "spp"]))
        # Sensors report at a finite resolution. Without this, decaying return flow with pumps
        # off stays at 1e-17 instead of 0 and a model can learn that artefact (it did: the
        # stuck-pipe detector lost its evidence when a WITSML feed rounded it to 0).
        vals = {k: round(x / RESOLUTION[k]) * RESOLUTION[k] for k, x in vals.items()}
        self.t += timedelta(seconds=dt)
        return Tick(self.t, vals, frozenset(labels))


def generate(
    plan: Plan, seed: int = 0, dt: float = 5.0, start: datetime | None = None
) -> Iterator[Tick]:
    """Ticks every `dt` seconds from plan.start_md to plan.end_md."""
    rng = random.Random(seed)
    rig = _Rig(plan, rng, start or datetime(2026, 1, 1, tzinfo=UTC), dt)
    pending = sorted(plan.events, key=lambda e: e[1])
    stands = 0

    def run(op: str, seconds: float, **kw: float) -> Iterator[Tick]:
        for _ in range(max(1, int(seconds / dt))):
            yield rig.step(op, **kw)

    def drilling_episode(h: str) -> Iterator[Tick]:
        """A hazard while drilling, then the crew's response."""
        if h == "kick":
            rig.effect(
                "kick", rng.uniform(600, 1500), q_out=rng.uniform(0.05, 0.25), spp=-0.03, tau=240
            )
            yield from run("drill", 900, wob=rng.uniform(60, 120), rpm=120)
            rig.effects = [e for e in rig.effects if e["label"] != "kick"]
            yield from run("shut_in", rng.uniform(1800, 3600))  # shut in, kill
            yield from bottom()
        elif h == "loss":
            sev = rng.uniform(0.1, 0.9)
            rig.effect("loss", rng.uniform(900, 2700), q_out=-sev, spp=-0.05 * sev, tau=90)
            yield from run("drill", 600, wob=rng.uniform(60, 120), rpm=120)
            yield from run("circ", 1800, rpm=60)  # LCM pill, observe
            yield from bottom()
        elif h == "pack_off":
            rig.effect(
                "pack_off",
                rng.uniform(300, 900),
                spp=rng.uniform(0.1, 0.4),
                torque=rng.uniform(0.2, 0.6),
                torque_noise=0.15,
                q_out=-0.1,
                tau=60,
            )
            yield from run("drill", 600, wob=rng.uniform(60, 120), rpm=120)
            rig.effects = [e for e in rig.effects if e["label"] != "pack_off"]
            yield from run("ream", 600, v=-0.05, rpm=80)
            yield from bottom()
        elif h == "washout":
            rig.effect("washout", 7200, spp=-rng.uniform(0.08, 0.2), tau=rng.uniform(1200, 3000))
            yield from run("drill", 5400, wob=rng.uniform(60, 120), rpm=120)

    def connection(stuck: bool) -> Iterator[Tick]:
        yield from run("circ", rng.uniform(60, 240), rpm=60)
        yield from run("ream", 45, v=-0.4, rpm=60)
        yield from run("slips", rng.uniform(1500, 2700) if stuck else rng.uniform(120, 360))
        if stuck:  # differential sticking: cannot pick up
            rig.effect(
                "stuck_pipe",
                rng.uniform(600, 1800),
                overpull=rng.uniform(0.3, 0.7),
                stuck=1,
                tau=20,
            )
            while any(e["label"] == "stuck_pipe" for e in rig.effects):
                yield rig.step("trip_out", v=-0.2)
        yield from bottom()

    def bottom() -> Iterator[Tick]:
        while rig.bit < rig.hole - 0.3:
            yield rig.step("trip_in", v=min(0.4, (rig.hole - rig.bit) / dt))

    def wiper(tight: bool) -> Iterator[Tick]:
        n = rng.randint(3, 8)
        tight_at = rig.bit - rng.uniform(10, n * STAND_M - 30) if tight else None
        for _ in range(n):  # out
            top = rig.bit - STAND_M
            while rig.bit > top + 0.5:
                active = tight_at is not None and rig.bit - 60 < tight_at <= rig.bit
                label = "tight_hole" if active else None
                if active and not any(e["label"] == "tight_hole" for e in rig.effects):
                    rig.effect(label, rng.uniform(60, 200), overpull=rng.uniform(0.1, 0.3), tau=10)
                yield rig.step("trip_out", v=-rng.uniform(0.25, 0.45))
            yield from run("slips", rng.uniform(60, 150))
        for _ in range(n):  # back in
            top = rig.bit + STAND_M
            while rig.bit < min(top, rig.hole - 0.6):
                yield rig.step("trip_in", v=rng.uniform(0.3, 0.5))
            yield from run("slips", rng.uniform(60, 150))
        yield from run("circ", rng.uniform(300, 900), rpm=60)
        yield from bottom()

    yield from run("circ", 300, rpm=60)
    yield from run("trip_in", 30, v=0.3)
    while rig.hole < plan.end_md:
        stand_top = rig.hole + STAND_M
        # One drilling hazard per stand at most, chosen in proportion to the rates.
        weights = [plan.rates.get(h, 0.0) for h in HAZARDS[:4]]
        hazard = (
            rng.choices(HAZARDS[:4], weights)[0] if rng.random() < min(sum(weights), 1) else None
        )
        # One episode per stand; a second event in the same stand waits for the next one.
        if pending and pending[0][1] <= stand_top and pending[0][0] in HAZARDS[:4]:
            hazard = pending.pop(0)[0]
        if rng.random() < 0.15:  # pump-rate change mid-stand
            rig.q_nominal *= rng.uniform(0.85, 1.15)
        slide = rng.random() < 0.15
        while rig.hole < stand_top:
            yield rig.step(
                "slide" if slide else "drill",
                wob=rng.uniform(60, 120),
                rpm=0 if slide else rng.uniform(100, 140),
            )
            if hazard and rig.hole > stand_top - STAND_M / 2:
                yield from drilling_episode(hazard)
                hazard = None
        stands += 1
        stuck = rng.random() < plan.rates.get("stuck_pipe", 0.0)
        while pending and pending[0][1] <= rig.hole and pending[0][0] == "stuck_pipe":
            pending.pop(0)
            stuck = True
        yield from connection(stuck)
        tight = False
        while pending and pending[0][1] <= rig.hole and pending[0][0] == "tight_hole":
            pending.pop(0)
            tight = True
        if tight or (plan.wiper_every and stands % plan.wiper_every == 0):
            yield from wiper(tight or rng.random() < plan.wiper_tight)
        while pending and pending[0][1] <= rig.hole:  # other types (cement, fishing): not simulated
            pending.pop(0)


def plan_from_db(conn: Any, wellbore_id: int, start_md: float | None = None) -> Plan:
    """Plan for a stored (synthetic) wellbore: its survey, tops, casing shoes, events and mud.
    Events of the simulated hazard types happen when the bit reaches their depth."""
    survey = [
        (float(md), float(inc), float(azi))
        for md, inc, azi in conn.execute(
            "SELECT md, inc, azi FROM survey_station WHERE wellbore_id = %s ORDER BY md",
            (wellbore_id,),
        )
    ]
    tops = [
        (str(n), float(md))
        for n, md in conn.execute(
            "SELECT f.name, t.md FROM formation_top t JOIN formation f ON f.id = t.formation_id"
            " WHERE t.wellbore_id = %s AND t.md IS NOT NULL ORDER BY t.md",
            (wellbore_id,),
        )
    ]
    shoes = [
        (float(md), float(od))
        for md, od in conn.execute(
            "SELECT md_shoe, od_in FROM casing_string WHERE wellbore_id = %s ORDER BY md_shoe",
            (wellbore_id,),
        )
    ]
    events = [
        (str(t), float(md))
        for t, md in conn.execute(
            "SELECT type, md_top FROM event WHERE wellbore_id = %s AND md_top IS NOT NULL"
            " ORDER BY md_top",
            (wellbore_id,),
        )
    ]
    row = conn.execute(
        "SELECT max(md) FROM (SELECT md FROM survey_station WHERE wellbore_id = %(w)s"
        " UNION ALL SELECT md_shoe FROM casing_string WHERE wellbore_id = %(w)s"
        " UNION ALL SELECT md_top FROM event WHERE wellbore_id = %(w)s) d",
        {"w": wellbore_id},
    ).fetchone()
    td = float(row[0]) if row and row[0] else 3000.0
    mw = conn.execute(
        "SELECT mw FROM mud_record WHERE wellbore_id = %s ORDER BY ts DESC LIMIT 1",
        (wellbore_id,),
    ).fetchone()
    start = start_md if start_md is not None else (shoes[0][0] + 5 if shoes else 500.0)
    return Plan(
        name=f"wellbore {wellbore_id}",
        survey=survey,
        tops=tops,
        shoes=[s for s in shoes if s[0] <= start],
        events=[e for e in events if e[1] > start],
        start_md=start,
        end_md=td,
        mw_sg=float(mw[0]) if mw else 1.2,
    )
