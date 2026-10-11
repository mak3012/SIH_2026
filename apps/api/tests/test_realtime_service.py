"""Real-time service end to end against a database (TEST_DATABASE_URL):

simulator (WITSML 1.4.1.1 store, 600x) -> WitsmlPoller -> Service -> rt_measurement, alerts,
NOTIFY -> WebSocket. The LLM explainer is a real HTTP service that is killed mid-replay: alerts
must still be raised and delivered. A paused feed must turn stale within 10 s.
"""

import asyncio
import json
import random
import socket
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import psycopg
import pytest
import uvicorn
from fastapi.testclient import TestClient
from fastapi.websockets import WebSocketDisconnect
from psycopg import AsyncConnection

from api.realtime import simulator as sm
from api.realtime import store
from api.realtime.channels import Sample
from api.realtime.connectors import WitsmlPoller
from api.realtime.context import context_from_plan
from api.realtime.service import Service, ServiceConfig, channel
from api.realtime.synthetic import generate, random_plan
from core.llm import LLM, JsonlLog, LLMConfig, PromptSet

B1, B2 = "AA-ONN-2001/1", "AA-ONN-2001/2"
PROMPTS = PromptSet(
    Path(__file__).resolve().parents[1] / "src" / "api" / "prompts", {"alert_explainer": "v2"}
)


def headers(
    roles: str = "planner,wellsite_engineer,data_steward", blocks: str = B1
) -> dict[str, str]:
    return {"X-User": "driller", "X-Roles": roles, "X-Blocks": blocks}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def world(db_url: str) -> Iterator[dict[str, Any]]:
    mp = pytest.MonkeyPatch()
    mp.setenv("DATABASE_URL", db_url)
    mp.setenv("DEV_AUTH", "1")
    from api import seed

    seed.main()
    with psycopg.connect(db_url) as c:
        rows = c.execute(
            "SELECT wb.id, w.block_id FROM wellbore wb JOIN well w ON w.id = wb.well_id"
            " ORDER BY wb.id"
        ).fetchall()
    yield {
        "url": db_url,
        "b1": next(i for i, b in rows if b == B1),
        "b2": next(i for i, b in rows if b == B2),
    }
    mp.undo()


class FakeOllama(BaseHTTPRequestHandler):
    """An LLM service that answers /api/chat with a valid explanation (no numbers, no
    citations, so it always verifies)."""

    def do_POST(self) -> None:  # noqa: N802
        self.rfile.read(int(self.headers["Content-Length"]))
        body = json.dumps(
            {
                "message": {
                    "content": json.dumps(
                        {
                            "why": "Return flow exceeds flow in while the pits gain volume.",
                            "citations": [],
                        }
                    )
                }
            }
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self) -> None:  # noqa: N802  (/api/tags for the model version)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"models": []}')

    def log_message(self, *a: Any) -> None:
        pass


def test_writes_are_idempotent_per_channel(world: dict[str, Any]) -> None:
    wb, ts = world["b1"], datetime(2025, 1, 1, tzinfo=UTC)

    async def go() -> None:
        async with await AsyncConnection.connect(world["url"], autocommit=True) as conn:
            a = Sample(wb, ts, {"bit_md": 100.0, "spp": 150.0})
            b = Sample(wb, ts, {"hookload": 800.0, "pit_vol": 40.0})
            await store.write(conn, [store.row(a, "circulating", {"mse": 50.0})] * 2)
            await store.write(conn, [store.row(b)])  # second connector, same timestamp
            await store.write(conn, [store.row(a, "circulating", {"mse": 50.0})])

    asyncio.run(go())
    with psycopg.connect(world["url"]) as c:
        rows = c.execute(
            "SELECT bit_md, spp_bar, hookload_kn, rig_state::text, channels FROM rt_measurement"
            " WHERE wellbore_id = %s AND ts = %s",
            (wb, ts),
        ).fetchall()
    assert rows == [(100.0, 150.0, 800.0, "circulating", {"mse": 50.0, "pit_vol": 40.0})]


def test_alerts_fire_when_the_llm_dies_and_stale_feed_is_flagged(
    world: dict[str, Any], tmp_path: Path
) -> None:
    wb = world["b1"]
    plan = random_plan(random.Random(42), "RT-TEST", 4.0)
    plan.rates, plan.wiper_every = {}, 0
    plan.events = [("kick", plan.start_md + 30), ("loss", plan.start_md + 75)]
    plan.end_md = plan.start_md + 110  # ~10 h of data, ~1 min at 600x
    ticks = [(t.ts, t.values) for t in generate(plan, seed=42)]
    sim = sm.Simulator(iter(ticks), speed=600)
    port, llm_port = free_port(), free_port()
    llm_server = HTTPServer(("127.0.0.1", llm_port), FakeOllama)
    threading.Thread(target=llm_server.serve_forever, daemon=True).start()
    llm = LLM(
        LLMConfig(backend="ollama", url=f"http://127.0.0.1:{llm_port}", model="m", timeout_s=3),
        JsonlLog(tmp_path / "llm.jsonl"),
        prompts=PROMPTS,
    )
    poller = WitsmlPoller(wb, f"http://127.0.0.1:{port}/witsml/store", *sim.uids, interval_s=0.2)
    # As if the roadmap put this well in Barail with a 40 % chance of losses and of kicks.
    ctx = context_from_plan(plan, wb)
    ctx.tops = [("Barail", plan.start_md - 50), ("Kopili", plan.start_md + 400)]
    ctx.priors = {("Barail", h): (0.4, "test roadmap") for h in ("kick", "loss")}
    svc = Service(world["url"], wb, poller, None, llm, ServiceConfig(explain_timeout_s=5), ctx=ctx)
    events: list[tuple[float, dict[str, Any]]] = []
    timeline: dict[str, float] = {}

    async def listen(conn: AsyncConnection) -> None:
        async for n in conn.notifies():
            events.append((time.monotonic(), json.loads(n.payload)))

    async def scenario() -> None:
        server = uvicorn.Server(uvicorn.Config(sm.make_app(sim), port=port, log_level="warning"))
        lconn = await AsyncConnection.connect(world["url"], autocommit=True)
        await lconn.execute(f"LISTEN {channel(wb)}")
        tasks = [
            asyncio.create_task(x) for x in (server.serve(), listen(lconn), sim.run(), svc.run())
        ]
        try:
            # Kill the LLM service as soon as it has explained one alert.
            while svc.stats["explained"] == 0 and not sim.done:
                await asyncio.sleep(0.1)
            llm_server.shutdown()
            llm_server.server_close()
            timeline["llm_killed"] = time.monotonic()
            while not sim.done or svc.stats["samples"] < len(ticks):
                await asyncio.sleep(0.2)
            await asyncio.sleep(1)
            # Freeze the feed: the service must flag it stale in under 10 s.
            sim.paused = True
            timeline["paused"] = time.monotonic()
            while svc.state != "stale" and time.monotonic() - timeline["paused"] < 15:
                await asyncio.sleep(0.1)
            timeline["stale"] = time.monotonic()
            while not svc.queue.empty():
                await asyncio.sleep(0.2)
            await asyncio.sleep(1)
        finally:
            server.should_exit = True
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await lconn.close()

    asyncio.run(asyncio.wait_for(scenario(), 300))

    with psycopg.connect(world["url"]) as c:
        alerts = c.execute(
            "SELECT id, hazard::text, tier, explanation_status, suppressed, prior, lr, posterior"
            " FROM alert WHERE wellbore_id = %s AND rule_id LIKE 'RT-%%' ORDER BY id",
            (wb,),
        ).fetchall()
        n_rows = c.execute(
            "SELECT count(*) FROM rt_measurement WHERE wellbore_id = %s AND ts > %s",
            (wb, datetime(2026, 1, 1, tzinfo=UTC)),
        ).fetchone()[0]  # type: ignore[index]
        feed_state, gaps = c.execute(
            "SELECT state, (SELECT count(*) FROM rt_gap g"
            " WHERE g.feed_id = f.id AND kind = 'stale')"
            " FROM rt_feed f WHERE wellbore_id = %s",
            (wb,),
        ).fetchone()  # type: ignore[misc]
    hazards = {h for _, h, *_ in alerts}
    assert {"kick", "loss"} <= hazards, alerts
    assert max(t for _, h, t, *_ in alerts if h == "kick") >= 2
    assert "done" in {a[3] for a in alerts}, alerts  # explained while the LLM was up
    assert {a[3] for a in alerts} & {"failed", "timeout"}, alerts  # ... and after it died
    # Every delivered alert was published, before any explanation of it.
    raised = {
        e["alert"]["id"]: t for t, e in events if e["type"] == "alert" and e["event"] == "raise"
    }
    explained = {
        e["alert"]["id"]: t for t, e in events if e["type"] == "alert" and e["event"] == "explained"
    }
    for aid, _, _, _, suppressed, *_ in alerts:
        if suppressed is None:
            assert aid in raised
            if aid in explained:
                assert raised[aid] <= explained[aid]
    after_kill = [aid for aid, t in raised.items() if t > timeline["llm_killed"]]
    assert after_kill, "no alert raised after the LLM was killed"
    # Bayes in the stored numbers (the database checks it too).
    for *_, prior, lr, post in alerts:
        assert post == pytest.approx(prior * lr / (prior * lr + 1 - prior), abs=1e-4)
    assert n_rows == len(ticks)  # one row per sample, however often the poller saw it
    assert timeline["stale"] - timeline["paused"] < 10
    assert gaps == 1 and feed_state == "stopped"  # stale was recorded; the test then stopped it
    assert any(e["type"] == "feed" and e["state"] == "stale" for _, e in events)


def test_websocket_streams_and_turns_stale(world: dict[str, Any]) -> None:
    from api.main import app

    wb = world["b1"]
    with TestClient(app) as client:
        with client.websocket_connect(f"/active/{wb}/stream", headers=headers(blocks=B2)) as ws:
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_json()
            assert closed.value.code == 4404  # another block's wellbore: not found
        with client.websocket_connect(f"/active/{wb}/stream", headers=headers()) as ws:
            snap = ws.receive_json()
            assert snap["type"] == "snapshot" and snap["wellbore_id"] == wb
            with psycopg.connect(world["url"], autocommit=True) as c:
                c.execute(
                    "SELECT pg_notify(%s, %s)",
                    (
                        channel(wb),
                        json.dumps(
                            {
                                "type": "sample",
                                "ts": "2026-10-01T00:00:00+00:00",
                                "values": {"bit_md": 1.0},
                            }
                        ),
                    ),
                )
            got = [ws.receive_json() for _ in range(2)]
            assert [m["type"] for m in got] == ["feed", "sample"]  # live again, then the data
            t0 = time.monotonic()
            msg = ws.receive_json()  # nothing else arrives: the API itself declares stale
            assert msg["type"] == "feed" and msg["state"] == "stale"
            assert time.monotonic() - t0 < 10


def test_websocket_token_message_and_bad_auth(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from api.main import app

    with TestClient(app) as client, client.websocket_connect(f"/active/{world['b1']}/stream") as ws:
        ws.send_json({"type": "auth", "token": "not-a-jwt"})
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
        assert closed.value.code == 4401


def test_shelve_ack_feedback(world: dict[str, Any]) -> None:
    from api.main import app

    wb = world["b1"]
    with psycopg.connect(world["url"]) as c:
        aid = c.execute(
            "INSERT INTO alert (wellbore_id, hazard, rule_id, rule_version, tier, prior, lr,"
            " posterior, evidence) VALUES (%s, 'kick', 'RT-KICK', 'test', 2, 0.01, 99, 0.5, '{}')"
            " RETURNING id",
            (wb,),
        ).fetchone()[0]  # type: ignore[index]
    with TestClient(app) as client:
        r = client.post(
            f"/active/{wb}/shelves",
            json={"hazard": "kick", "reason": "no", "minutes": 30},
            headers=headers(),
        )
        assert r.status_code == 422  # reason too short
        r = client.post(
            f"/active/{wb}/shelves",
            json={"hazard": "kick", "reason": "pit transfer in progress", "minutes": 721},
            headers=headers(),
        )
        assert r.status_code == 422  # more than one shift
        r = client.post(
            f"/active/{wb}/shelves",
            json={"hazard": "kick", "reason": "pit transfer in progress", "minutes": 30},
            headers=headers(),
        )
        assert r.status_code == 200
        shelf = r.json()["id"]
        assert (
            client.post(
                f"/active/{wb}/shelves",
                json={"hazard": "kick", "reason": "pit transfer", "minutes": 30},
                headers=headers(roles="planner,data_steward,admin"),
            ).status_code
            == 403
        )
        assert client.post(f"/alerts/{aid}/ack", headers=headers(blocks=B2)).status_code == 404
        assert (
            client.post(f"/alerts/{aid}/ack", headers=headers()).json()["status"] == "acknowledged"
        )
        assert client.post(f"/alerts/{aid}/ack", headers=headers()).status_code == 409
        r = client.post(
            f"/alerts/{aid}/feedback",
            json={"feedback": "false_positive", "note": "transfer"},
            headers=headers(),
        )
        assert r.status_code == 200
        listed = client.get(f"/active/{wb}/alerts", headers=headers()).json()
        assert any(a["id"] == aid and a["status"] == "acknowledged" for a in listed)
        assert client.delete(f"/shelves/{shelf}", headers=headers()).status_code == 200
        assert client.delete(f"/shelves/{shelf}", headers=headers()).status_code == 404
        assert client.get(f"/active/{wb}/feed", headers=headers()).status_code == 200
    with psycopg.connect(world["url"]) as c:
        actions = [
            a
            for (a,) in c.execute(
                "SELECT action FROM audit_log WHERE action LIKE 'alert.%%' ORDER BY id"
            )
        ]
    assert actions[-4:] == ["alert.shelve", "alert.ack", "alert.feedback", "alert.unshelve"]


def test_alert_summary_carries_rule_sources_without_an_explanation() -> None:
    from api.realtime.service import alert_summary

    ev = {
        "roadmap_evidence": [{"well": f"SA-0{i}", "page": i, "source_ref_id": i} for i in range(5)]
    }
    a = {
        "id": 1,
        "hazard": "loss",
        "tier": 2,
        "posterior": 0.6,
        "prior": 0.2,
        "lr": 6.0,
        "evidence": ev,
    }
    s = alert_summary(a)
    assert s["citations"] is None and [x["id"] for x in s["sources"]] == ["R1", "R2", "R3"]
    assert s["sources"][0]["source_ref_id"] == 0
