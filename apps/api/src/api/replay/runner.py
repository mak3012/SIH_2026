"""Replay one held-out well through the live path and record what it did.

    synthetic real-time data for the well (synthetic.plan_from_db: its survey, casing and the
    DDR-recorded events, each happening when the bit reaches its depth)
      -> WITSML 1.4.1.1 simulator (HTTP) -> WitsmlPoller -> realtime.Service
         (idempotent writes, rig state, derived channels, detectors, fusion, alerts, NOTIFY)
    priors: leave-one-out roadmap (priors.py), never the held-out well's own events.

Only depth windows around the recorded events are replayed (120 m above to 40 m below each,
merged when they overlap; a 250 m section when the well has no events), each as its own stretch
of rig time separated by a two-hour feed gap, so a well's events are covered in hours of data
instead of weeks. Every sample in those windows counts towards false-alert hours.

Each sample's risk (posterior per hazard), prior, rig state and depth, and every alert action,
are recorded through the service's observer.
"""

import asyncio
import socket
import sys
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import uvicorn

from api.realtime import context
from api.realtime.connectors import WitsmlPoller
from api.realtime.features import HAZARDS
from api.realtime.fusion import posterior
from api.realtime.pipeline import Detectors, Step
from api.realtime.service import Service, ServiceConfig
from api.realtime.simulator import Simulator, make_app
from api.realtime.synthetic import Plan, generate, plan_from_db
from api.replay.priors import loo_priors

ABOVE_M, BELOW_M, QUIET_M = 120.0, 40.0, 250.0
GAP = timedelta(hours=2)
STALL_S = 120.0  # all data served and no sample processed for this long: stop and report
START = datetime(2030, 1, 1, tzinfo=UTC)  # generation clock; ticks() shifts it to end now


@dataclass
class Episode:
    hazard: str
    onset: datetime
    end: datetime
    md: float


@dataclass
class WellResult:
    wellbore_id: int
    well: str
    target: str | None
    offsets: list[tuple[str, float]]
    windows: list[tuple[float, float]]
    samples: int
    hours: float
    operating_hours: float
    episodes: list[Episode] = field(default_factory=list)
    alerts: list[dict[str, Any]] = field(default_factory=list)  # delivered raise/reopen/escalate
    timeline: list[dict[str, Any]] = field(default_factory=list)  # one row per evaluation
    priors: dict[str, float] = field(default_factory=dict)  # "formation|hazard" -> p_formation
    events: list[dict[str, Any]] = field(default_factory=list)  # DB events (the DDR record)
    wall_s: float = 0.0
    lost: int = 0  # samples served but never processed (a connector error); should be 0

    def json(self) -> dict[str, Any]:
        return asdict(self)


def windows(events: list[tuple[str, float]], start: float, td: float) -> list[tuple[float, float]]:
    spans = sorted(
        (max(start, md - ABOVE_M), min(td, md + BELOW_M)) for _, md in events if md > start
    )
    out: list[tuple[float, float]] = []
    for a, b in spans:
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        elif b > a:
            out.append((a, b))
    if not out:
        mid = max(start, (start + td) / 2 - QUIET_M / 2)
        out = [(mid, min(td, mid + QUIET_M))]
    return out


def ticks(
    plan: Plan, spans: list[tuple[float, float]], seed: int
) -> list[tuple[datetime, dict[str, float], frozenset[str]]]:
    out: list[tuple[datetime, dict[str, float], frozenset[str]]] = []
    t = START
    for i, (a, b) in enumerate(spans):
        p = replace(
            plan,
            start_md=a,
            end_md=b,
            events=[e for e in plan.events if a < e[1] <= b],
            shoes=[s for s in plan.shoes if s[0] <= a],
        )
        for tk in generate(p, seed=seed + i, start=t):
            out.append((tk.ts, tk.values, tk.labels))
        t = out[-1][0] + GAP
    # Recorded history ending now: screens show today's dates and the shift digest's "last
    # 12 h" holds the replay's alerts. Durations and lead times do not change.
    if out:
        shift = datetime.now(UTC).replace(microsecond=0) - out[-1][0]
        out = [(ts + shift, v, lab) for ts, v, lab in out]
    return out


def episodes(rows: list[tuple[datetime, dict[str, float], frozenset[str]]]) -> list[Episode]:
    out: list[Episode] = []
    open_: dict[str, Episode] = {}
    for ts, vals, labels in rows:
        for h in labels:
            if h not in open_:
                open_[h] = Episode(h, ts, ts, float(vals.get("bit_md", 0.0)))
                out.append(open_[h])
            open_[h].end = ts
        for h in [h for h in open_ if h not in labels]:
            del open_[h]
    return out


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def reset(conn: psycopg.Connection, wellbore_id: int) -> None:
    """Remove earlier replays of this wellbore. Refused unless all its documents are synthetic:
    the replay never deletes real data."""
    real = conn.execute(
        "SELECT count(*) FROM source_ref r JOIN document d ON d.id = r.document_id"
        " JOIN wellbore b ON b.source_ref_id = r.id WHERE b.id = %s AND NOT d.synthetic",
        (wellbore_id,),
    ).fetchone()
    if real and real[0]:
        raise PermissionError(f"wellbore {wellbore_id} is not synthetic; replay will not reset it")
    conn.execute(
        "DELETE FROM alert WHERE wellbore_id = %s AND rule_id LIKE 'RT-%%'", (wellbore_id,)
    )
    for t in ("rt_gap", "rt_feed", "rt_measurement"):
        conn.execute(f"DELETE FROM {t} WHERE wellbore_id = %s", (wellbore_id,))


async def replay_well(
    dsn: str,
    wellbore_id: int,
    detectors: Detectors | None,
    speed: float | None,
    seed: int = 0,
    llm: Any = None,
    focus: str | None = None,
) -> WellResult:
    """`llm`: explain delivered alerts like the live service (demo); None in the evaluation.
    `focus`: a hazard; replay only from FOCUS_BEFORE before its first recorded episode to
    FOCUS_AFTER after it (demo: the alert comes within a minute, whatever the CPU)."""
    t_wall = asyncio.get_running_loop().time()
    with psycopg.connect(dsn) as conn:
        reset(conn, wellbore_id)
        name = conn.execute(
            "SELECT w.name FROM wellbore b JOIN well w ON w.id = b.well_id WHERE b.id = %s",
            (wellbore_id,),
        ).fetchone()[0]  # type: ignore[index]
        loo = loo_priors(conn, wellbore_id)
        ctx = context.load(conn, wellbore_id)
        ctx.priors, ctx.roadmap_evidence = loo.priors, loo.evidence
        base = plan_from_db(conn, wellbore_id)
        td = base.end_md
        db_events = [
            {"id": i, "type": t, "md": float(md), "formation": f, "t_start": ts, "npt_hr": npt}
            for i, t, md, f, ts, npt in conn.execute(
                "SELECT e.id, e.type::text, e.md_top, f.name, e.t_start, e.npt_hr FROM event e"
                " LEFT JOIN formation f ON f.id = e.formation_id WHERE e.wellbore_id = %s"
                " ORDER BY e.md_top",
                (wellbore_id,),
            )
        ]
        conn.commit()
    plan = replace(base, rates={}, wiper_tight=0.0)
    spans = windows([(e["type"], e["md"]) for e in db_events], base.start_md, td)
    rows = ticks(plan, spans, seed)
    eps = episodes(rows)
    if focus is not None:
        ep = next((e for e in eps if e.hazard == focus), None)
        if ep is None:
            raise SystemExit(f"{name} has no recorded {focus} event to focus on")
        rows = [r for r in rows if ep.onset - FOCUS_BEFORE <= r[0] <= ep.end + FOCUS_AFTER]
        eps = episodes(rows)

    sim = Simulator(
        iter([(ts, v) for ts, v, _ in rows]),
        speed=speed,
        rebase=False,
        uid_well=name,
        uid_wellbore=name,
        uid_log="REPLAY",
    )
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(make_app(sim), host="127.0.0.1", port=port, log_level="warning")
    )
    poller = WitsmlPoller(
        wellbore_id,
        f"http://127.0.0.1:{port}/witsml/store",
        *sim.uids,
        interval_s=0.05 if speed is None else 0.5,
    )
    timeline: list[dict[str, Any]] = []
    alerts: list[dict[str, Any]] = []
    tiers: dict[int, int] = {}

    def observe(sample: Any, step: Step) -> None:
        for a in step.actions:
            if a.kind in ("raise", "reopen", "escalate") and a.delivered:
                alerts.append(
                    {
                        "hazard": a.hazard,
                        "ts": a.ts,
                        "tier": a.tier,
                        "kind": a.kind,
                        "posterior": round(a.posterior, 4),
                        "key": a.key,
                    }
                )
            tiers[a.key] = a.tier
        if step.hazards:
            timeline.append(
                {
                    "ts": step.ts,
                    "md": sample.values.get("bit_md"),
                    "state": step.rig_state,
                    "formation": step.formation,
                    "post": {
                        h: round(posterior(i["prior"], i["lr"]), 5) for h, i in step.hazards.items()
                    },
                    "prior": {h: i["prior"] for h, i in step.hazards.items()},
                    "gated": [h for h, i in step.hazards.items() if i["gated"]],
                }
            )

    states: dict[str, int] = {}

    def count_states(sample: Any, step: Step) -> None:
        states[step.rig_state] = states.get(step.rig_state, 0) + 1
        observe(sample, step)

    svc = Service(
        dsn,
        wellbore_id,
        poller,
        detectors,
        llm,
        ServiceConfig(stale_after_s=30.0),
        ctx=ctx,
        feed_config={"replay": True},
        observer=count_states,
    )
    tasks = [asyncio.create_task(x) for x in (server.serve(), sim.run(), svc.run())]
    try:
        seen, still = -1, 0.0
        while svc.stats["samples"] < len(rows):
            if any(t.done() and t.exception() for t in tasks):
                raise next(t.exception() for t in tasks if t.done() and t.exception())  # type: ignore[misc]
            await asyncio.sleep(0.2)
            still = still + 0.2 if svc.stats["samples"] == seen else 0.0
            seen = svc.stats["samples"]
            if sim.done and still > STALL_S:  # a sample was lost; do not wait for ever
                break
        # Demo (llm): let queued explanations finish, so none is left "pending".
        waited = 0.0
        while llm is not None and waited < svc.cfg.explain_timeout_s + 5:
            st = svc.stats
            if st["explained"] + st["explain_failed"] >= st["queued"]:
                break
            await asyncio.sleep(0.5)
            waited += 0.5
    finally:
        server.should_exit = True
        for t in tasks[1:]:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    dt_h = 5.0 / 3600  # synthetic samples every 5 s
    operating = sum(n for s, n in states.items() if s not in ("static", "unknown")) * dt_h
    return WellResult(
        wellbore_id,
        name,
        loo.target,
        loo.offsets,
        spans,
        len(rows),
        len(rows) * dt_h,
        operating,
        eps,
        alerts,
        timeline,
        {f"{f}|{h}": p for (f, h), (p, _) in loo.priors.items() if h in HAZARDS},
        db_events,
        asyncio.get_running_loop().time() - t_wall,
        lost=len(rows) - svc.stats["samples"],
    )


FOCUS_BEFORE = timedelta(minutes=20)  # enough for the detectors' rolling windows
FOCUS_AFTER = timedelta(minutes=10)


def run_sync(args: tuple[Any, ...]) -> dict[str, Any]:
    """Process entry point for parallel evaluation: (dsn, wellbore id, detector path, speed,
    seed[, explain[, focus]]) -> WellResult as a dict. `explain`: an LLM from LLM_BACKEND /
    LLM_URL / LLM_MODEL explains delivered alerts, as on a rig (the demo; never in the
    evaluation). `focus`: see replay_well."""
    dsn, wellbore_id, bank_path, speed, seed, *rest = args
    from pathlib import Path

    from api.realtime.detectors import load

    bank = load(Path(bank_path)) if bank_path else None
    llm = None
    if rest and rest[0]:
        from core.llm import LLM, LLMConfig, PgLog, PromptSet

        prompts = PromptSet(
            Path(__file__).resolve().parents[1] / "prompts", {"alert_explainer": "v2"}
        )
        llm = LLM(
            LLMConfig.from_env(),
            PgLog(lambda: psycopg.connect(dsn, autocommit=True)),
            prompts=prompts,
        )
    factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    with asyncio.Runner(loop_factory=factory) as r:
        focus = rest[1] if len(rest) > 1 else None
        return r.run(replay_well(dsn, wellbore_id, bank, speed, seed, llm, focus)).json()
