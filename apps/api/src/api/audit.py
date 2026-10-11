"""Audit log: who read, changed or decided what, and which model versions ran.

`audit_log` (migration 0005) is append-only and hash-chained: each row's hash covers the row
and the previous hash, so editing, deleting or re-ordering rows breaks `audit_verify()`.
Truncating the tail does not break the chain, so the head is anchored outside the database:

    python -m api.audit anchor >> audit-anchors.jsonl       # hourly; keep the file off the box
    python -m api.audit verify --anchors - < audit-anchors.jsonl

`verify` exits 1 when the chain is broken, an anchored row changed, or rows past an anchor are
gone. Needs DATABASE_URL (the owner: the app roles may append but not read the log).

What is recorded (docs/security.md#audit): reads of source documents, page images, voice
recordings and briefing PDFs, assistant questions with the records they cited, every edit and
decision (offset selections, programs, lint runs, briefings, review decisions, voice notes),
acknowledgements, shelves and alert feedback, and model versions when a service loads them.
"""

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb


def record(
    conn: psycopg.Connection,
    actor: str,
    action: str,
    entity: str | None = None,
    entity_id: object = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """Append one entry, in the caller's transaction (so it commits or rolls back with the
    action it records)."""
    conn.execute(
        "INSERT INTO audit_log (actor, action, entity, entity_id, payload)"
        " VALUES (%s, %s, %s, %s, %s)",
        (
            actor,
            action,
            entity,
            None if entity_id is None else str(entity_id),
            Jsonb(payload or {}),
        ),
    )


def status(conn: psycopg.Connection) -> dict[str, Any]:
    """Entries, head and the first broken id (None when intact). Callable by app_rw through the
    SECURITY DEFINER function audit_status() (migration 0015)."""
    row = conn.execute("SELECT * FROM audit_status()").fetchone()
    assert row is not None
    n, head_id, head_hash, broken = row
    return {"entries": n, "head_id": head_id, "head_hash": head_hash, "first_broken": broken}


def anchor(conn: psycopg.Connection) -> dict[str, Any]:
    """The current head, as one line of an anchor file."""
    s = status(conn)
    return {"at": datetime.now(UTC).isoformat(), "id": s["head_id"], "hash": s["head_hash"]}


def verify(conn: psycopg.Connection, anchors: list[dict[str, Any]]) -> list[str]:
    """Problems with the chain and the anchors; empty when all is well."""
    s = status(conn)
    problems = []
    if s["first_broken"] is not None:
        problems.append(f"chain broken at id {s['first_broken']}")
    for a in anchors:
        if a["id"] is None:
            continue
        row = conn.execute(
            "SELECT encode(payload_hash, 'hex') FROM audit_log WHERE id = %s", (a["id"],)
        ).fetchone()
        if row is None:
            problems.append(f"anchored entry {a['id']} ({a['at']}) is missing: log truncated")
        elif row[0] != a["hash"]:
            problems.append(f"anchored entry {a['id']} ({a['at']}) has a different hash")
    return problems


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m api.audit")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("anchor")
    sub.add_parser("verify").add_argument("--anchors", help="anchor file, or - for stdin")
    a = p.parse_args()
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        if a.cmd == "anchor":
            print(json.dumps(anchor(conn)))
            return
        text = ""
        if a.anchors == "-":
            text = sys.stdin.read()
        elif a.anchors:
            text = Path(a.anchors).read_text(encoding="utf-8")
        anchors = [json.loads(line) for line in text.splitlines() if line.strip()]
        problems = verify(conn, anchors)
        print(json.dumps({**status(conn), "anchors": len(anchors), "problems": problems}))
        sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
