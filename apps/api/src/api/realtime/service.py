"""The real-time service for one wellbore: connector -> store -> pipeline -> alerts ->
notifications, plus heartbeat/staleness and the asynchronous LLM explainer.

Order of operations for an alert (docs/realtime.md has the sequence diagram):
1. the pipeline's rules decide (fusion.AlarmManager) and return an action;
2. the alert row is INSERTed and committed (autocommit) - it exists;
3. it is published (NOTIFY rt_wellbore_<id>) - it is delivered;
4. only then is its id queued for the explainer. The explainer runs in a worker task with a
   timeout; failure or timeout sets explanation_status and nothing else.

Runs as the database owner (an ingest service, like the pipelines), not under a user's RLS
scope. The API's WebSocket checks the user's access before it LISTENs.
"""

import asyncio
import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg
from opentelemetry import trace
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from api.realtime import context, store
from api.realtime.connectors import Connector
from api.realtime.explainer import explain
from api.realtime.fusion import TIER_NAMES, Action
from api.realtime.pipeline import Context, Detectors, Pipeline, Step
from core.llm import LLM
from core.telemetry import ALERT_EVAL, FEED_LATENCY, tracer

log = logging.getLogger(__name__)
RULESET = "realtime@1.0.0"
NOTIFY_MAX = 7900  # NOTIFY payloads must stay under 8000 bytes
SAMPLE_KEYS = ("ecd", "mse", "dxc", "delta_flow", "hookload_pred", "hookload_dev", "stick_s")


def channel(wellbore_id: int) -> str:
    return f"rt_wellbore_{wellbore_id}"


@dataclass(frozen=True)
class ServiceConfig:
    stale_after_s: float = 6.0  # wall clock without new data -> DATA STALE (UI grey < 10 s)
    notify_interval_s: float = 0.5  # sample messages at most this often (alerts always)
    # a slow CPU-only model needs more (the demo sets 120); the alert never waits for it
    explain_timeout_s: float = float(os.environ.get("RT_EXPLAIN_TIMEOUT_S", "30"))
    shelves_refresh_s: float = 10.0
    context_refresh_s: float = 600.0
    reconnect_max_s: float = 30.0


def alert_summary(a: dict[str, Any]) -> dict[str, Any]:
    ev = a.get("evidence") or {}
    return {
        "id": a["id"],
        "hazard": a["hazard"],
        "tier": a["tier"],
        "tier_name": TIER_NAMES.get(a["tier"]),
        "posterior": round(a["posterior"], 4),
        "prior": a["prior"],
        "lr": round(a["lr"], 2),
        "raised_at": a["raised_at"].isoformat() if a.get("raised_at") else None,
        "cleared_at": a["cleared_at"].isoformat() if a.get("cleared_at") else None,
        "formation": a.get("formation"),
        "rig_state": a.get("rig_state"),
        "features": ev.get("features"),
        "explanation_status": a.get("explanation_status"),
        "explanation": a.get("llm_explanation"),
        "citations": a.get("llm_citations"),
        # The offset records behind the prior (rules, not the LLM): the alert shows its source
        # pages even when no explanation is written.
        "sources": [
            {"id": f"R{i}", **e} for i, e in enumerate(ev.get("roadmap_evidence") or [], 1)
        ][:3],
    }


def payload(msg: dict[str, Any]) -> str:
    s = json.dumps(msg, default=str)
    if len(s) > NOTIFY_MAX and msg.get("type") == "alert":
        msg = {**msg, "alert": {k: v for k, v in msg["alert"].items() if k != "citations"}}
        s = json.dumps(msg, default=str)
    return s[:NOTIFY_MAX] if len(s) > NOTIFY_MAX else s


class Service:
    def __init__(
        self,
        dsn: str,
        wellbore_id: int,
        connector: Connector,
        detectors: Detectors | None = None,
        llm: LLM | None = None,
        cfg: ServiceConfig | None = None,
        ctx: Context | None = None,
        feed_config: dict[str, Any] | None = None,
        observer: Callable[[Any, Step], None] | None = None,
    ) -> None:
        """`observer(sample, step)` sees every processed sample (the replay evaluation records
        risk scores with it); it must not block."""
        self.observer = observer
        self.dsn, self.wellbore_id, self.connector = dsn, wellbore_id, connector
        self.detectors, self.llm = detectors, llm
        self.cfg = cfg or ServiceConfig()
        self.ctx = ctx
        self.feed_config = feed_config or {}
        self.state = "disconnected"
        self.keys: dict[int, int] = {}  # alarm manager key -> alert id
        self.queue: asyncio.Queue[int] = asyncio.Queue()
        self.last_rx = 0.0
        self.last_notify = 0.0
        self.last_feed_write = 0.0
        self.stale_gap: int | None = None
        self.stats = {"samples": 0, "alerts": 0, "queued": 0, "explained": 0, "explain_failed": 0}

    # ------------------------------------------------------------------ setup
    async def _connect(self) -> AsyncConnection:
        return await AsyncConnection.connect(self.dsn, autocommit=True)

    def _load_context(self) -> Context:
        with psycopg.connect(self.dsn) as conn:
            return context.load(conn, self.wellbore_id)

    async def run(self) -> None:
        self.conn = await self._connect()
        if self.ctx is None:
            self.ctx = await asyncio.to_thread(self._load_context)
        self.pipe = Pipeline(self.ctx, self.detectors)
        row = await (
            await self.conn.execute(
                "INSERT INTO rt_feed (wellbore_id, connector, config, state) VALUES (%s, %s, %s,"
                " 'disconnected') ON CONFLICT (wellbore_id, connector) DO UPDATE SET"
                " config = EXCLUDED.config, updated_at = now() RETURNING id",
                (self.wellbore_id, self.connector.kind, Jsonb(self.feed_config)),
            )
        ).fetchone()
        self.feed_id = int(row[0]) if row else 0
        await self.conn.execute(
            "INSERT INTO audit_log (actor, action, entity, entity_id, payload)"
            " VALUES ('realtime', 'model.load', 'rt_feed', %s, %s)",
            (str(self.feed_id), Jsonb(self.models())),
        )
        await self._adopt_open_alerts()
        await self._refresh_shelves()
        tasks = [
            asyncio.create_task(self._consume(), name="consume"),
            asyncio.create_task(self._watchdog(), name="watchdog"),
            asyncio.create_task(self._explainer(), name="explainer"),
            asyncio.create_task(self._housekeeping(), name="housekeeping"),
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()
            await self._set_state("stopped")
            await self.conn.close()

    def models(self) -> dict[str, Any]:
        """Versions of everything that decides or explains an alert (audited at start)."""
        d, llm = self.detectors, self.llm
        return {
            "wellbore_id": self.wellbore_id,
            "ruleset": RULESET,
            "detectors": None
            if d is None
            else {"version": d.version, "sha256": getattr(d, "sha256", None)},
            "llm": None
            if llm is None or not llm.enabled
            else {
                "backend": llm.cfg.backend,
                "model": llm.cfg.model,
                "prompts": llm.prompts.versions if llm.prompts else {},
            },
        }

    async def _adopt_open_alerts(self) -> None:
        cur = await self.conn.execute(
            "SELECT id, hazard::text, tier, coalesce(peak_posterior, posterior) FROM alert"
            " WHERE wellbore_id = %s AND cleared_at IS NULL AND status IN ('open', 'acknowledged')"
            " AND rule_id LIKE 'RT-%%' ORDER BY id",
            (self.wellbore_id,),
        )
        for aid, hazard, tier, peak in await cur.fetchall():
            self.keys[self.pipe.alarms.adopt(hazard, tier, peak)] = aid

    async def _refresh_shelves(self) -> None:
        cur = await self.conn.execute(
            "SELECT DISTINCT ON (hazard) hazard::text, id, expires_at FROM alert_shelf"
            " WHERE wellbore_id = %s AND unshelved_at IS NULL AND expires_at > now()"
            " ORDER BY hazard, expires_at DESC",
            (self.wellbore_id,),
        )
        self.pipe.alarms.shelves = {h: (i, e) for h, i, e in await cur.fetchall()}

    async def _housekeeping(self) -> None:
        last_ctx = time.monotonic()
        while True:
            await asyncio.sleep(self.cfg.shelves_refresh_s)
            await self._refresh_shelves()
            if time.monotonic() - last_ctx > self.cfg.context_refresh_s:
                last_ctx = time.monotonic()
                try:
                    self.pipe.set_context(await asyncio.to_thread(self._load_context))
                except Exception:
                    log.exception("context reload failed; keeping the old one")

    # ------------------------------------------------------------------ feed state
    async def notify(self, msg: dict[str, Any]) -> None:
        await self.conn.execute(
            "SELECT pg_notify(%s, %s)", (channel(self.wellbore_id), payload(msg))
        )

    async def _set_state(self, state: str) -> None:
        if state == self.state:
            return
        self.state = state
        await self.conn.execute(
            "UPDATE rt_feed SET state = %s, updated_at = now() WHERE id = %s", (state, self.feed_id)
        )
        await self.notify({"type": "feed", "state": state, "at": datetime.now(UTC).isoformat()})

    async def _watchdog(self) -> None:
        while True:
            await asyncio.sleep(0.5)
            if self.state == "live" and time.monotonic() - self.last_rx > self.cfg.stale_after_s:
                self.pipe.pause()
                cur = await self.conn.execute(
                    "INSERT INTO rt_gap (wellbore_id, feed_id, kind, gap_start)"
                    " VALUES (%s, %s, 'stale', now()) RETURNING id",
                    (self.wellbore_id, self.feed_id),
                )
                row = await cur.fetchone()
                self.stale_gap = int(row[0]) if row else None
                await self._set_state("stale")

    async def _consume(self) -> None:
        backoff = 1.0
        while True:
            try:
                async for s in self.connector.stream():
                    await self.handle(s)
                    backoff = 1.0
                return  # finite source (replay) ended
            except NotImplementedError:
                raise
            except Exception as e:
                log.warning(
                    "connector %s failed: %s; retrying in %.0f s", self.connector.kind, e, backoff
                )
                await self._set_state("disconnected")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.cfg.reconnect_max_s)

    # ------------------------------------------------------------------ samples
    async def handle(self, s: Any) -> Step:
        """One sample through the alert path, timed (ALERT_EVAL, FEED_LATENCY) and traced."""
        t0 = time.monotonic()
        with tracer.start_as_current_span(
            "rt.sample", attributes={"rt.wellbore_id": self.wellbore_id}
        ) as span:
            step = await self._handle(s, t0)
            span.set_attribute("rt.actions", len(step.actions))
        ALERT_EVAL.observe(time.monotonic() - t0)
        ts = s.ts if s.ts.tzinfo else s.ts.replace(tzinfo=UTC)
        lag = (datetime.now(UTC) - ts).total_seconds()
        FEED_LATENCY.labels(self.connector.kind).observe(max(lag, 0.0))
        return step

    async def _handle(self, s: Any, now: float) -> Step:
        self.last_rx = now
        if self.state != "live":
            if self.stale_gap is not None:
                await self.conn.execute(
                    "UPDATE rt_gap SET gap_end = now() WHERE id = %s", (self.stale_gap,)
                )
                self.stale_gap = None
            await self._set_state("live")
        step = self.pipe.push(s)
        self.stats["samples"] += 1
        if step.gap_s is not None:
            await self.conn.execute(
                "INSERT INTO rt_gap (wellbore_id, feed_id, kind, gap_start, gap_end)"
                " VALUES (%s, %s, 'data_gap', %s::timestamptz - make_interval(secs => %s), %s)",
                (self.wellbore_id, self.feed_id, s.ts, step.gap_s, s.ts),
            )
        await store.write(self.conn, [store.row(s, step.rig_state, step.derived)])
        if now - self.last_feed_write > 1.0:
            self.last_feed_write = now
            await self.conn.execute(
                "UPDATE rt_feed SET last_sample_ts = %s, last_rx_at = now(), updated_at = now()"
                " WHERE id = %s",
                (s.ts, self.feed_id),
            )
        for a in step.actions:
            await self.apply(a, step)
        if self.observer is not None:
            self.observer(s, step)
        if now - self.last_notify >= self.cfg.notify_interval_s:
            self.last_notify = now
            await self.notify(
                {
                    "type": "sample",
                    "ts": s.ts.isoformat(),
                    "values": {k: round(v, 3) for k, v in s.values.items()},
                    "derived": {
                        k: round(step.derived[k], 4) for k in SAMPLE_KEYS if k in step.derived
                    },
                    "rig_state": step.rig_state,
                    "formation": step.formation,
                    "posterior": {
                        h: round(i["prior"] * i["lr"] / (i["prior"] * i["lr"] + 1 - i["prior"]), 4)
                        for h, i in step.hazards.items()
                    },
                }
            )
        return step

    # ------------------------------------------------------------------ alerts
    async def _alert(self, alert_id: int) -> dict[str, Any]:
        cur = await self.conn.execute(
            "SELECT id, hazard::text, tier, prior, lr, posterior, raised_at, cleared_at, formation,"
            " rig_state::text, evidence, explanation_status, llm_explanation, llm_citations,"
            " suppressed FROM alert WHERE id = %s",
            (alert_id,),
        )
        row = await cur.fetchone()
        cols = [d.name for d in cur.description or []]
        a = dict(zip(cols, row, strict=True)) if row else {}
        return {**a, "tier_name": TIER_NAMES.get(a.get("tier", 0))}

    async def apply(self, a: Action, step: Step) -> None:
        ev = a.evidence
        if a.kind == "raise":
            explaining = a.delivered and self.llm is not None and self.llm.enabled
            status = "pending" if explaining else ("disabled" if a.delivered else "none")
            cur = await self.conn.execute(
                "INSERT INTO alert (wellbore_id, raised_at, hazard, rule_id, rule_version, tier,"
                " prior, lr, posterior, evidence, formation, rig_state, last_seen_at,"
                " peak_posterior, suppressed, shelf_id, detector_version, calibrated,"
                " explanation_status) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
                " %s::rig_state, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (
                    self.wellbore_id,
                    a.ts,
                    a.hazard,
                    f"RT-{a.hazard.upper()}",
                    RULESET,
                    a.tier,
                    a.prior,
                    a.lr,
                    a.posterior,
                    Jsonb(ev),
                    step.formation,
                    step.rig_state,
                    a.ts,
                    a.posterior,
                    a.suppressed,
                    a.shelf_id,
                    self.detectors.version if self.detectors else None,
                    bool(ev.get("detector", {}).get("calibrated")),
                    status,
                ),
            )
            row = await cur.fetchone()
            assert row is not None
            aid = self.keys[a.key] = int(row[0])
            self.stats["alerts"] += 1
            trace.get_current_span().add_event(
                "alert.raise",
                {"alert.id": aid, "alert.hazard": a.hazard, "alert.tier": a.tier},
            )
            if a.delivered:
                await self.notify(
                    {
                        "type": "alert",
                        "event": "raise",
                        "alert": alert_summary(await self._alert(aid)),
                    }
                )
                if explaining:
                    self.queue.put_nowait(aid)  # after the alert is stored and delivered
                    self.stats["queued"] += 1
            return
        known = self.keys.get(a.key)
        if known is None:
            return
        aid = known
        if a.kind in ("reopen", "escalate"):
            await self.conn.execute(
                "UPDATE alert SET tier = %s, prior = %s, lr = %s, posterior = %s,"
                " peak_posterior = greatest(coalesce(peak_posterior, 0), %s), last_seen_at = %s,"
                " cleared_at = NULL, updates = updates + 1, suppressed = %s,"
                " shelf_id = %s, evidence = %s WHERE id = %s",
                (
                    a.tier,
                    a.prior,
                    a.lr,
                    a.posterior,
                    a.posterior,
                    a.ts,
                    a.suppressed,
                    a.shelf_id,
                    Jsonb(ev),
                    aid,
                ),
            )
        elif a.kind == "update":
            await self.conn.execute(
                "UPDATE alert SET peak_posterior = greatest(coalesce(peak_posterior, 0), %s),"
                " last_seen_at = %s WHERE id = %s",
                (a.posterior, a.ts, aid),
            )
            return  # not worth a message: the sample stream carries the posterior
        elif a.kind == "clear":
            await self.conn.execute(
                "UPDATE alert SET cleared_at = %s, last_seen_at = %s WHERE id = %s",
                (a.ts, a.ts, aid),
            )
        full = await self._alert(aid)
        if full.get("suppressed") is None:
            await self.notify({"type": "alert", "event": a.kind, "alert": alert_summary(full)})
            if a.kind in ("reopen", "escalate") and full["explanation_status"] in (
                "none",
                "disabled",
            ):
                if self.llm is not None and self.llm.enabled:
                    await self.conn.execute(
                        "UPDATE alert SET explanation_status = 'pending' WHERE id = %s", (aid,)
                    )
                    self.queue.put_nowait(aid)
                    self.stats["queued"] += 1

    # ------------------------------------------------------------------ explainer
    async def _explainer(self) -> None:
        while True:
            aid = await self.queue.get()
            alert = await self._alert(aid)
            status, error, result = "failed", None, {}
            span = tracer.start_span("rt.explain", attributes={"alert.id": aid})
            try:
                assert self.llm is not None
                with trace.use_span(span):  # the LLM call's span is a child of this one
                    result = await asyncio.wait_for(
                        asyncio.to_thread(explain, self.llm, alert), self.cfg.explain_timeout_s
                    )
                status = result["status"]
                error = "; ".join(result.get("problems", [])) or None
            except TimeoutError:
                status, error = "timeout", f"no answer in {self.cfg.explain_timeout_s:g} s"
            except Exception as e:  # the alert stands whatever the explainer does
                status, error = "failed", f"{type(e).__name__}: {e}"[:500]
            span.set_attribute("explanation.status", status)
            span.end()
            try:
                if status == "done":
                    await self.conn.execute(
                        "UPDATE alert SET llm_explanation = %s, llm_model = %s, llm_citations = %s,"
                        " explanation_status = 'done', explanation_error = NULL WHERE id = %s",
                        (result["why"], result["model"], Jsonb(result["citations"]), aid),
                    )
                    self.stats["explained"] += 1
                else:
                    await self.conn.execute(
                        "UPDATE alert SET explanation_status = %s, explanation_error = %s"
                        " WHERE id = %s",
                        (status, error, aid),
                    )
                    self.stats["explain_failed"] += 1
                await self.notify(
                    {
                        "type": "alert",
                        "event": "explained",
                        "alert": alert_summary(await self._alert(aid)),
                    }
                )
            except Exception:
                log.exception("could not record the explanation of alert %s", aid)
