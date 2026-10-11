import json
from collections.abc import Iterator

import psycopg
import pytest
from psycopg.types.json import Jsonb

from api import audit
from api.db import scoped


def _log(conn: psycopg.Connection, n: int) -> list[int]:
    ids = []
    for i in range(n):
        row = conn.execute(
            "INSERT INTO audit_log (actor, action, entity, entity_id, payload)"
            " VALUES ('tester', 'update', 'event', %s, %s) RETURNING id",
            (str(i), Jsonb({"field": "npt_hr", "old": i, "new": i + 1})),
        ).fetchone()
        assert row is not None
        ids.append(row[0])
    return ids


def _verify(conn: psycopg.Connection) -> int | None:
    row = conn.execute("SELECT audit_verify()").fetchone()
    assert row is not None
    return row[0]


@pytest.fixture
def tx(conn: psycopg.Connection) -> Iterator[psycopg.Connection]:
    """Owner transaction, rolled back afterwards so tampering never persists."""
    conn.autocommit = False
    yield conn
    conn.rollback()


def test_chain_links_and_verifies(tx: psycopg.Connection) -> None:
    ids = _log(tx, 3)
    rows = tx.execute(
        "SELECT id, prev_hash, payload_hash FROM audit_log WHERE id = ANY(%s) ORDER BY id", (ids,)
    ).fetchall()
    assert [r[0] for r in rows] == sorted(ids)
    for prev, cur in zip(rows, rows[1:], strict=False):
        assert cur[1] == prev[2]
    assert _verify(tx) is None


def test_update_and_delete_are_refused(tx: psycopg.Connection) -> None:
    (i,) = _log(tx, 1)
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        with tx.transaction():
            tx.execute("UPDATE audit_log SET actor = 'mallory' WHERE id = %s", (i,))
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        with tx.transaction():
            tx.execute("DELETE FROM audit_log WHERE id = %s", (i,))


@pytest.mark.parametrize(
    ("tamper", "first_bad"),
    [
        # An edited row fails its own hash.
        ("UPDATE audit_log SET payload = '{\"new\": 0}' WHERE id = %s", 1),
        # A deleted row breaks the next row's prev_hash link.
        ("DELETE FROM audit_log WHERE id = %s", 2),
    ],
)
def test_tampering_is_detected(tx: psycopg.Connection, tamper: str, first_bad: int) -> None:
    ids = _log(tx, 4)
    assert _verify(tx) is None
    tx.execute("ALTER TABLE audit_log DISABLE TRIGGER audit_immutable")
    tx.execute(tamper, (ids[1],))
    tx.execute("ALTER TABLE audit_log ENABLE TRIGGER audit_immutable")
    assert _verify(tx) == ids[first_bad]


def test_app_can_append_but_not_read(conn: psycopg.Connection) -> None:
    with scoped(conn, "app_rw", blocks=[]):
        conn.execute("INSERT INTO audit_log (actor, action) VALUES ('api', 'login')")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with scoped(conn, "app_rw", blocks=[]):
            conn.execute("SELECT * FROM audit_log")
    assert _verify(conn) is None


def test_anchor_detects_truncation(tx: psycopg.Connection) -> None:
    ids = _log(tx, 3)
    a = json.loads(json.dumps(audit.anchor(tx)))  # as stored in the anchor file
    assert a["id"] == ids[-1] and audit.verify(tx, [a]) == []
    # Deleting the tail keeps the chain intact, but not the anchor.
    tx.execute("ALTER TABLE audit_log DISABLE TRIGGER audit_immutable")
    tx.execute("DELETE FROM audit_log WHERE id = %s", (a["id"],))
    tx.execute("ALTER TABLE audit_log ENABLE TRIGGER audit_immutable")
    assert audit.status(tx)["first_broken"] is None
    assert audit.verify(tx, [a]) == [
        f"anchored entry {a['id']} ({a['at']}) is missing: log truncated"
    ]


def test_app_role_sees_status_only(conn: psycopg.Connection) -> None:
    with scoped(conn, "app_rw", blocks=[]):
        audit.record(conn, "api", "read.document", "document", 1)
        s = audit.status(conn)
    assert s["entries"] >= 1 and s["first_broken"] is None and len(s["head_hash"]) == 64
