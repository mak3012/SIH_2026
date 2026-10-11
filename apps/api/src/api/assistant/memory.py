"""Conversation memory per user. Every read and write runs as app_rw with the caller's blocks
and with app.user set, so RLS shows a user only their own conversations."""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from api import audit
from api.assistant.service import Result
from api.auth import User
from api.db import scoped

HISTORY = 3


@contextmanager
def session(user: User) -> Iterator[psycopg.Connection]:
    with (
        psycopg.connect(os.environ["DATABASE_URL"]) as conn,
        scoped(conn, "app_rw", user.blocks, user.jvs),
    ):
        conn.execute("SELECT set_config('app.user', %s, true)", (user.name,))
        yield conn


def context(
    user: User, conversation_id: int | None
) -> tuple[dict[str, Any] | None, list[tuple[str, str]]]:
    """(entities of the last answered turn, last turns as (question, answer)). LookupError when
    the conversation is not the caller's."""
    if conversation_id is None:
        return None, []
    with session(user) as conn:
        if (
            conn.execute(
                "SELECT 1 FROM assistant_conversation WHERE id = %s", (conversation_id,)
            ).fetchone()
            is None
        ):
            raise LookupError(conversation_id)
        rows = conn.execute(
            "SELECT question, answer, status, entities FROM assistant_turn"
            " WHERE conversation_id = %s ORDER BY id DESC LIMIT %s",
            (conversation_id, HISTORY),
        ).fetchall()
    previous = next((r[3] for r in rows if r[2] == "answered"), None)
    return previous, [(q, a) for q, a, _, _ in reversed(rows)]


def save(
    user: User, conversation_id: int | None, question: str, res: Result, mode: str
) -> tuple[int, int]:
    with session(user) as conn:
        if conversation_id is None:
            row = conn.execute(
                "INSERT INTO assistant_conversation (user_name, title) VALUES (%s, %s)"
                " RETURNING id",
                (user.name, question[:80]),
            ).fetchone()
            assert row is not None
            conversation_id = int(row[0])
        else:
            conn.execute(
                "UPDATE assistant_conversation SET updated_at = now() WHERE id = %s",
                (conversation_id,),
            )
        row = conn.execute(
            "INSERT INTO assistant_turn (conversation_id, user_name, question, input_mode, intent,"
            " status, answer, entities, citations, evidence, verification, followups, model,"
            " latency_ms) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " RETURNING id",
            (
                conversation_id,
                user.name,
                question,
                mode,
                res.intent,
                "answered" if res.status == "answered" else res.status,
                res.answer,
                Jsonb(res.entities),
                Jsonb(res.citations),
                Jsonb(res.evidence),
                Jsonb(res.verification),
                Jsonb(res.followups),
                res.model,
                res.latency_ms,
            ),
        ).fetchone()
        assert row is not None
        audit.record(
            conn,
            user.name,
            "assistant.ask",
            "assistant_turn",
            row[0],
            {
                "question": question,
                "mode": mode,
                "status": res.status,
                "cited": sorted(
                    {c["source_ref_id"] for c in res.citations if c.get("source_ref_id")}
                ),
                "model": res.model,
            },
        )
        return conversation_id, int(row[0])


def conversations(user: User) -> list[dict[str, Any]]:
    with session(user) as conn:
        rows = conn.execute(
            "SELECT c.id, c.title, c.created_at, c.updated_at, count(t.id)"
            " FROM assistant_conversation c"
            " LEFT JOIN assistant_turn t ON t.conversation_id = c.id GROUP BY c.id"
            " ORDER BY c.updated_at DESC LIMIT 100"
        ).fetchall()
    keys = ["id", "title", "created_at", "updated_at", "turns"]
    return [dict(zip(keys, r, strict=True)) for r in rows]


def turns(user: User, conversation_id: int) -> list[dict[str, Any]]:
    with session(user) as conn:
        cur = conn.execute(
            "SELECT id, question, input_mode, intent, status, answer, citations, followups,"
            " created_at FROM assistant_turn WHERE conversation_id = %s ORDER BY id",
            (conversation_id,),
        )
        cols = [d.name for d in cur.description or []]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def forget(user: User, conversation_id: int) -> bool:
    with session(user) as conn:
        return (
            conn.execute(
                "DELETE FROM assistant_conversation WHERE id = %s RETURNING id", (conversation_id,)
            ).fetchone()
            is not None
        )
