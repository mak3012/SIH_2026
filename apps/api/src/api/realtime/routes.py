"""Real-time HTTP and WebSocket endpoints.

WebSocket /active/{wellbore_id}/stream
    Authenticate with an `Authorization: Bearer` header, or (browsers cannot set headers on a
    WebSocket) by sending {"type": "auth", "token": "..."} as the first message within 5 s.
    The wellbore must be visible under the caller's RLS scope, else close code 4404 (4401 for
    bad auth). Then: one `snapshot`, followed by `sample`, `alert` and `feed` messages from
    the real-time service (LISTEN rt_wellbore_<id>). The API watches the stream itself: no
    sample for STALE_AFTER_S seconds -> {"type": "feed", "state": "stale"} even when the service
    is dead and cannot say so.

Alert lifecycle: acknowledge, feedback, shelve (with reason, at most one shift), unshelve.
"""

import asyncio
import json
import os
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from psycopg import AsyncConnection
from pydantic import BaseModel, Field

from api import audit
from api.auth import CurrentUser, User, authenticate
from api.db import scoped
from api.intelligence.routes import Conn
from api.realtime.features import HAZARDS
from api.realtime.service import alert_summary, channel

router = APIRouter(tags=["realtime"])
STALE_AFTER_S = float(os.environ.get("RT_STALE_AFTER_S", "6"))
AUTH_TIMEOUT_S = 5.0
ALERT_COLS = (
    "id, hazard::text, tier, prior, lr, posterior, raised_at, cleared_at, formation,"
    " rig_state::text, evidence, explanation_status, llm_explanation, llm_citations, status::text,"
    " ack_by, ack_at, suppressed"
)


def _alerts(
    conn: psycopg.Connection, wellbore_id: int, hours: float, suppressed: bool
) -> list[dict[str, Any]]:
    cur = conn.execute(
        f"SELECT {ALERT_COLS} FROM alert WHERE wellbore_id = %s"
        " AND (cleared_at IS NULL OR raised_at > now() - %s * interval '1 hour')"
        " AND (%s OR suppressed IS NULL) ORDER BY raised_at DESC LIMIT 200",
        (wellbore_id, hours, suppressed),
    )
    cols = [d.name for d in cur.description or []]
    out = []
    for r in cur.fetchall():
        a = dict(zip(cols, r, strict=True))
        out.append(
            {
                **alert_summary(a),
                "status": a["status"],
                "ack_by": a["ack_by"],
                "suppressed": a["suppressed"],
            }
        )
    return out


def _snapshot(user: User, wellbore_id: int) -> dict[str, Any] | None:
    with (
        psycopg.connect(os.environ["DATABASE_URL"]) as conn,
        scoped(conn, "app_rw", user.blocks, user.jvs),
    ):
        if conn.execute("SELECT 1 FROM wellbore WHERE id = %s", (wellbore_id,)).fetchone() is None:
            return None
        last = conn.execute(
            "SELECT ts, rig_state::text, bit_md, hole_md, hookload_kn, wob_kn, rpm, torque_knm,"
            " spp_bar, flow_in_lpm, mw_in, rop_m_hr, ecd, channels FROM rt_measurement"
            " WHERE wellbore_id = %s ORDER BY ts DESC LIMIT 1",
            (wellbore_id,),
        ).fetchone()
        feed = conn.execute(
            "SELECT connector, state, last_sample_ts, last_rx_at FROM rt_feed"
            " WHERE wellbore_id = %s"
            " ORDER BY updated_at DESC LIMIT 1",
            (wellbore_id,),
        ).fetchone()
        alerts = _alerts(conn, wellbore_id, 12, False)
        # The stream's samples carry the formation; the snapshot looks it up from the tops.
        at = (
            conn.execute(
                "SELECT f.name FROM formation_top t JOIN formation f ON f.id = t.formation_id"
                " JOIN source_ref r ON r.id = t.source_ref_id WHERE t.wellbore_id = %s"
                " AND r.review_status <> 'rejected' AND t.md <= %s ORDER BY t.md DESC LIMIT 1",
                (wellbore_id, last[2]),
            ).fetchone()
            if last and last[2] is not None
            else None
        )
        formation = at[0] if at else None
    state = "disconnected"
    if feed:
        state = feed[1]
        if state == "live" and (
            feed[3] is None or datetime.now(UTC) - feed[3] > timedelta(seconds=STALE_AFTER_S)
        ):
            state = "stale"
    sample = None
    if last:
        keys = [
            "bit_md",
            "hole_md",
            "hookload",
            "wob",
            "rpm",
            "torque",
            "spp",
            "flow_in",
            "mw_in",
            "rop",
            "ecd",
        ]
        sample = {
            "ts": last[0].isoformat(),
            "rig_state": last[1],
            "values": {k: v for k, v in zip(keys, last[2:13], strict=True) if v is not None}
            | (last[13] or {}),
            "formation": formation,
        }
    return {
        "type": "snapshot",
        "wellbore_id": wellbore_id,
        "feed": {
            "state": state,
            "connector": feed[0] if feed else None,
            "last_sample_ts": feed[2].isoformat() if feed and feed[2] else None,
        },
        "sample": sample,
        "alerts": alerts,
        "stale_after_s": STALE_AFTER_S,
    }


async def _user(ws: WebSocket) -> User:
    if ws.headers.get("authorization") or (
        os.environ.get("DEV_AUTH") == "1" and ws.headers.get("x-user")
    ):
        return authenticate(ws.headers)
    msg = await asyncio.wait_for(ws.receive_json(), AUTH_TIMEOUT_S)
    if (
        not isinstance(msg, dict)
        or msg.get("type") != "auth"
        or not isinstance(msg.get("token"), str)
    ):
        raise HTTPException(401, "first message must be {type: auth, token}")
    return authenticate({"Authorization": f"Bearer {msg['token']}"})


@router.websocket("/active/{wellbore_id}/stream")
async def stream(ws: WebSocket, wellbore_id: int) -> None:
    await ws.accept()
    try:
        user = await _user(ws)
    except (HTTPException, TimeoutError, ValueError, WebSocketDisconnect):
        await ws.close(code=4401, reason="authentication failed")
        return
    snap = await asyncio.to_thread(_snapshot, user, wellbore_id)
    if snap is None:
        await ws.close(code=4404, reason="wellbore not found")
        return
    await ws.send_json(snap)
    state = snap["feed"]["state"]
    last_data = time.monotonic() if state == "live" else 0.0

    async with await AsyncConnection.connect(os.environ["DATABASE_URL"], autocommit=True) as lc:
        await lc.execute(f"LISTEN {channel(wellbore_id)}")

        async def forward() -> None:
            nonlocal state, last_data
            while True:
                async for n in lc.notifies(timeout=0.5):
                    msg = json.loads(n.payload)
                    if msg.get("type") == "sample":
                        last_data = time.monotonic()
                        if state != "live":
                            state = "live"
                            await ws.send_json({"type": "feed", "state": "live", "source": "api"})
                    elif msg.get("type") == "feed":
                        state = msg["state"]
                    await ws.send_json(msg)
                if state == "live" and time.monotonic() - last_data > STALE_AFTER_S:
                    state = "stale"
                    await ws.send_json(
                        {
                            "type": "feed",
                            "state": "stale",
                            "source": "api",
                            "at": datetime.now(UTC).isoformat(),
                        }
                    )

        async def receive() -> None:
            while True:
                await ws.receive_text()  # client messages are ignored; returns on disconnect

        tasks = [asyncio.create_task(forward()), asyncio.create_task(receive())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            for t in tasks:
                try:
                    await t
                except (asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
                    pass


# ------------------------------------------------------------------ REST


@router.get("/active/{wellbore_id}/alerts")
def list_alerts(
    wellbore_id: int,
    hours: float = Query(12, gt=0, le=24 * 30),
    include_suppressed: bool = False,
    conn: psycopg.Connection = Conn,
) -> list[dict[str, Any]]:
    """Open alerts and those raised in the last `hours`. Suppressed (shelved, over budget)
    alerts only on request: they are recorded but were not delivered."""
    return _alerts(conn, wellbore_id, hours, include_suppressed)


@router.get("/active/{wellbore_id}/feed")
def feed(wellbore_id: int, conn: psycopg.Connection = Conn) -> dict[str, Any]:
    feeds = conn.execute(
        "SELECT connector, state, last_sample_ts, last_rx_at, updated_at FROM rt_feed"
        " WHERE wellbore_id = %s ORDER BY updated_at DESC",
        (wellbore_id,),
    ).fetchall()
    gaps = conn.execute(
        "SELECT kind, gap_start, gap_end FROM rt_gap WHERE wellbore_id = %s"
        " ORDER BY gap_start DESC LIMIT 50",
        (wellbore_id,),
    ).fetchall()
    keys = ["connector", "state", "last_sample_ts", "last_rx_at", "updated_at"]
    return {
        "feeds": [dict(zip(keys, f, strict=True)) for f in feeds],
        "gaps": [{"kind": k, "start": s, "end": e} for k, s, e in gaps],
    }


@router.post("/alerts/{alert_id}/ack")
def acknowledge(
    alert_id: int, user: User = CurrentUser, conn: psycopg.Connection = Conn
) -> dict[str, Any]:
    user.require("operate")
    row = conn.execute("SELECT status::text FROM alert WHERE id = %s", (alert_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "alert not found")
    if row[0] != "open":
        raise HTTPException(409, f"alert is {row[0]}")
    conn.execute(
        "UPDATE alert SET status = 'acknowledged', ack_by = %s, ack_at = now() WHERE id = %s",
        (user.name, alert_id),
    )
    audit.record(conn, user.name, "alert.ack", "alert", alert_id, {})
    return {"id": alert_id, "status": "acknowledged", "ack_by": user.name}


class Feedback(BaseModel):
    feedback: str = Field(pattern="^(true_positive|false_positive|unsure)$")
    note: str | None = Field(None, max_length=2000)


@router.post("/alerts/{alert_id}/feedback")
def alert_feedback(
    alert_id: int, body: Feedback, user: User = CurrentUser, conn: psycopg.Connection = Conn
) -> dict[str, Any]:
    """Engineer's verdict on an alert: labels for the next detector training."""
    user.require("operate")
    if (
        conn.execute(
            "UPDATE alert SET feedback = %s, feedback_note = %s WHERE id = %s RETURNING id",
            (body.feedback, body.note, alert_id),
        ).fetchone()
        is None
    ):
        raise HTTPException(404, "alert not found")
    audit.record(conn, user.name, "alert.feedback", "alert", alert_id, body.model_dump())
    return {"id": alert_id, **body.model_dump()}


class ShelveRequest(BaseModel):
    hazard: str
    reason: str = Field(min_length=5, max_length=500)
    minutes: int = Field(60, ge=1, le=720)


@router.post("/active/{wellbore_id}/shelves")
def shelve(
    wellbore_id: int, req: ShelveRequest, user: User = CurrentUser, conn: psycopg.Connection = Conn
) -> dict[str, Any]:
    """Shelve a hazard's advisory and warning alerts for up to one shift (720 min), with a
    reason. Critical alerts are never shelved."""
    user.require("operate")
    if req.hazard not in HAZARDS:
        raise HTTPException(422, f"hazard must be one of {list(HAZARDS)}")
    if conn.execute("SELECT 1 FROM wellbore WHERE id = %s", (wellbore_id,)).fetchone() is None:
        raise HTTPException(404, "wellbore not found")
    row = conn.execute(
        "INSERT INTO alert_shelf (wellbore_id, hazard, reason, shelved_by, expires_at)"
        " VALUES (%s, %s, %s, %s, now() + make_interval(mins => %s)) RETURNING id, expires_at",
        (wellbore_id, req.hazard, req.reason.strip(), user.name, req.minutes),
    ).fetchone()
    assert row is not None
    audit.record(conn, user.name, "alert.shelve", "alert_shelf", row[0], req.model_dump())
    return {"id": row[0], "hazard": req.hazard, "expires_at": row[1], "shelved_by": user.name}


@router.delete("/shelves/{shelf_id}")
def unshelve(
    shelf_id: int, user: User = CurrentUser, conn: psycopg.Connection = Conn
) -> dict[str, Any]:
    user.require("operate")
    if (
        conn.execute(
            "UPDATE alert_shelf SET unshelved_by = %s, unshelved_at = now() WHERE id = %s"
            " AND unshelved_at IS NULL RETURNING id",
            (user.name, shelf_id),
        ).fetchone()
        is None
    ):
        raise HTTPException(404, "active shelf not found")
    audit.record(conn, user.name, "alert.unshelve", "alert_shelf", shelf_id, {})
    return {"id": shelf_id, "unshelved_by": user.name}
