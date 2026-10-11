"""Rig Edge Pack: the hazard package, the rig box's sync agent, and headquarters' endpoints.

A rig box runs the same stack (compose profile `edge`) on one machine, offline-first:

    HQ   python -m api.edge pack --wellbore-id 19 > pkg.tar.gz    (or make edge-package WELL=19)
    rig  python -m api.edge load pkg.tar.gz                        first load, e.g. from USB
    rig  python -m api.edge agent --interval 60                    the edge-sync service

The package holds what the rig needs to work with no link: every table the app reads (facts,
documents, offsets, roadmap, lessons, passages...), limited by row-level security to the
licence blocks and JVs of the wellbore and its selected offsets, plus the document and voice
files (`--no-files` for a refresh over VSAT) and the detector bank. Rows keep HQ's ids; loading
upserts them. Rows made on the rig (alerts, shelves, voice notes) get ids from 2^50 up
(edge_enable(), migration 0016), so the two never collide.

When HQ answers, the agent uploads what the outbox triggers queued and the audit entries since
the last upload, then refreshes the package every PACKAGE_EVERY_S. HQ maps rig ids to its own
(edge_record) so re-sent batches are idempotent, and checks that the rig's audit chain continues
from the last entry it received and that every entry's hash is right: an edited or missing
entry on the rig is refused, not merged. Full-rate time series stays on the rig (the rig's
WITSML store replicates to the operator's real-time centre); HQ gets the feed's state.
"""

import argparse
import base64
import hashlib
import io
import json
import logging
import os
import re
import ssl
import sys
import tarfile
import time
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import psycopg
from fastapi import APIRouter, Body, HTTPException, Query, Response
from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, ValidationError

from api import audit, storage
from api.auth import CurrentUser, User
from api.db import scoped
from api.intelligence.routes import Conn

log = logging.getLogger("api.edge")
router = APIRouter(prefix="/edge", tags=["edge"])

FORMAT = 1
EDGE_ID_BASE = 2**50
# Made on the rig, sent up, never packaged down; or private, or not the rig's business.
NOT_PACKAGED = {
    "alert", "alert_shelf", "rt_feed", "rt_gap", "rt_measurement", "assistant_conversation",
    "assistant_turn", "review_item", "edge_outbox", "edge_state", "edge_box", "edge_record",
}  # fmt: skip
UPLOADED = ("alert_shelf", "alert", "audio_note")  # order: alerts reference shelves
MAX_BATCH = 500


# ---------------------------------------------------------------- package
def _tables(conn: psycopg.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')"
        " AND has_table_privilege('app_rw', c.oid, 'SELECT') ORDER BY 1"
    ).fetchall()
    return [r[0] for r in rows if r[0] not in NOT_PACKAGED]


def _columns(conn: psycopg.Connection, table: str) -> list[str]:
    rows = conn.execute(
        "SELECT attname FROM pg_attribute WHERE attrelid = %s::regclass AND attnum > 0"
        " AND NOT attisdropped AND attgenerated = '' ORDER BY attnum",
        (table,),
    ).fetchall()
    return [r[0] for r in rows]


def _head(conn: psycopg.Connection) -> str:
    row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    assert row is not None
    return str(row[0])


def scope(conn: psycopg.Connection, wellbore_id: int) -> tuple[list[str], list[str]]:
    """Blocks and JVs of the wellbore and of its latest offset selection (owner connection)."""
    rows = conn.execute(
        "SELECT DISTINCT w.block_id, w.jv_id FROM well w WHERE w.id IN ("
        " SELECT b.well_id FROM wellbore b WHERE b.id = %(w)s"
        " UNION SELECT i.well_id FROM offset_selection_item i WHERE i.selection_id ="
        "  (SELECT max(id) FROM offset_selection WHERE active_wellbore_id = %(w)s))",
        {"w": wellbore_id},
    ).fetchall()
    if not rows:
        raise LookupError(f"wellbore {wellbore_id} not found")
    return sorted({r[0] for r in rows if r[0]}), sorted({r[1] for r in rows if r[1]})


def pack(
    conn: psycopg.Connection,
    wellbore_id: int,
    blocks: list[str],
    jvs: list[str],
    files: bool = True,
    detectors: Path | None = None,
) -> bytes:
    """The hazard package (tar.gz) as `blocks`/`jvs` may see it."""
    buf = io.BytesIO()
    members: dict[str, bytes] = {}
    manifest: dict[str, Any] = {
        "format": FORMAT,
        "created_at": datetime.now(UTC).isoformat(),
        "wellbore_id": wellbore_id,
        "blocks": blocks,
        "jvs": jvs,
        "alembic": _head(conn),
        "tables": {},
        "files": {},
    }
    tables = _tables(conn)
    with scoped(conn, "app_rw", blocks, jvs):
        if conn.execute("SELECT 1 FROM wellbore WHERE id = %s", (wellbore_id,)).fetchone() is None:
            raise LookupError(f"wellbore {wellbore_id} is not in blocks {blocks}")
        for t in tables:
            cols = _columns(conn, t)
            q = sql.SQL("COPY (SELECT {} FROM {}) TO STDOUT (FORMAT csv, HEADER)").format(
                sql.SQL(", ").join(map(sql.Identifier, cols)), sql.Identifier(t)
            )
            out = io.BytesIO()
            with conn.cursor().copy(q) as cp:
                for chunk in cp:
                    out.write(chunk)
            members[f"tables/{t}.csv"] = out.getvalue()
            manifest["tables"][t] = cols
        if files:
            for kind, table in (("document", "document"), ("audio", "audio_note")):
                for oid, uri in conn.execute(
                    sql.SQL("SELECT id, uri FROM {} WHERE uri IS NOT NULL").format(
                        sql.Identifier(table)
                    )
                ).fetchall():
                    try:
                        data = storage.read(str(uri))
                    except Exception:  # not stored (synthetic records without a file)
                        continue
                    name = f"files/{kind}/{oid}"
                    members[name] = data
                    manifest["files"][name] = {"table": table, "id": oid, "uri": uri}
    if detectors is not None and detectors.exists():
        members["models/detectors.joblib"] = detectors.read_bytes()
        members["models/detectors.sha256"] = detectors.with_suffix(".sha256").read_bytes()
    manifest["sha256"] = {k: hashlib.sha256(v).hexdigest() for k, v in members.items()}
    members["manifest.json"] = json.dumps(manifest, indent=1, default=str).encode()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _pk(conn: psycopg.Connection, table: str) -> list[str]:
    rows = conn.execute(
        "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid"
        " AND a.attnum = ANY(i.indkey) WHERE i.indrelid = %s::regclass AND i.indisprimary",
        (table,),
    ).fetchall()
    return [r[0] for r in rows]


def load(conn: psycopg.Connection, data: bytes, models_dir: Path | None = None) -> dict[str, Any]:
    """Upsert a package into this (rig) database; owner connection (a superuser: triggers and
    foreign keys are deferred to the end with session_replication_role). Returns counts."""
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        m = {i.name: tar.extractfile(i).read() for i in tar.getmembers() if i.isfile()}  # type: ignore[union-attr]
    manifest = json.loads(m.pop("manifest.json"))
    if manifest["format"] != FORMAT:
        raise ValueError(f"package format {manifest['format']}, expected {FORMAT}")
    for name, digest in manifest["sha256"].items():
        if hashlib.sha256(m[name]).hexdigest() != digest:
            raise ValueError(f"{name}: checksum mismatch")
    if manifest["alembic"] != _head(conn):
        raise ValueError(
            f"package schema {manifest['alembic']}, this database {_head(conn)}: upgrade first"
        )
    # The manifest names tables, columns and files: only what a package may contain.
    allowed = set(_tables(conn))
    for t, cols in manifest["tables"].items():
        if t not in allowed or not set(cols) <= set(_columns(conn, t)):
            raise ValueError(f"package table {t} is not one a package may load")
    for f in manifest["files"].values():
        if f["table"] not in ("document", "audio_note"):
            raise ValueError(f"package file for table {f['table']}")
    counts: dict[str, int] = {}
    with conn.transaction():
        conn.execute("SET LOCAL session_replication_role = replica")
        for t, cols in manifest["tables"].items():
            ident = sql.SQL(", ").join(map(sql.Identifier, cols))
            pk = _pk(conn, t)
            conn.execute(
                sql.SQL("CREATE TEMP TABLE _in ON COMMIT DROP AS SELECT {} FROM {} LIMIT 0").format(
                    ident, sql.Identifier(t)
                )
            )
            with conn.cursor().copy(
                sql.SQL("COPY _in ({}) FROM STDIN (FORMAT csv, HEADER)").format(ident)
            ) as cp:
                cp.write(m[f"tables/{t}.csv"])
            update = [c for c in cols if c not in pk]
            action = (
                sql.SQL("DO UPDATE SET {}").format(
                    sql.SQL(", ").join(
                        sql.SQL("{0} = EXCLUDED.{0}").format(sql.Identifier(c)) for c in update
                    )
                )
                if update
                else sql.SQL("DO NOTHING")
            )
            cur = conn.execute(
                sql.SQL(
                    "INSERT INTO {} ({}) OVERRIDING SYSTEM VALUE SELECT {} FROM _in"
                    " ON CONFLICT ({}) {}"
                ).format(
                    sql.Identifier(t),
                    ident,
                    ident,
                    sql.SQL(", ").join(map(sql.Identifier, pk)),
                    action,
                )
            )
            counts[t] = cur.rowcount
            conn.execute("DROP TABLE _in")
        for name, f in manifest["files"].items():
            uri = f["uri"]
            if not str(uri).startswith("s3://"):  # stored elsewhere at HQ: give it a home here
                uri = f"s3://edge/{name.removeprefix('files/')}"
                conn.execute(
                    sql.SQL("UPDATE {} SET uri = %s WHERE id = %s").format(
                        sql.Identifier(f["table"])
                    ),
                    (uri, f["id"]),
                )
            bucket, key = str(uri).removeprefix("s3://").split("/", 1)
            storage.put(bucket, key, m[name], "application/octet-stream")
        conn.execute(
            "INSERT INTO edge_state VALUES ('package', %s)"
            " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (Jsonb({k: v for k, v in manifest.items() if k not in ("sha256", "tables", "files")}),),
        )
        conn.execute("SELECT edge_enable()")
    if models_dir is not None and "models/detectors.joblib" in m:
        models_dir.mkdir(parents=True, exist_ok=True)
        (models_dir / "detectors.joblib").write_bytes(m["models/detectors.joblib"])
        (models_dir / "detectors.sha256").write_bytes(m["models/detectors.sha256"])
    return {"tables": counts, "files": len(manifest["files"]), "created_at": manifest["created_at"]}


# ---------------------------------------------------------------- rig -> HQ batch
def batch(conn: psycopg.Connection, edge_id: str, wellbore_id: int) -> tuple[dict[str, Any], int]:
    """What to upload now (owner connection on the rig) and the last outbox id it covers."""
    rows = conn.execute(
        "SELECT entity, entity_id, max(id) FROM edge_outbox GROUP BY entity, entity_id"
        " ORDER BY max(id) LIMIT %s",
        (MAX_BATCH,),
    ).fetchall()
    upto = max((r[2] for r in rows), default=0)
    records: dict[str, list[dict[str, Any]]] = {}
    for entity in UPLOADED:
        ids = [r[1] for r in rows if r[0] == entity]
        if not ids:
            continue
        got = conn.execute(
            sql.SQL("SELECT row_to_json(t) FROM {} t WHERE id = ANY(%s) ORDER BY id").format(
                sql.Identifier(entity)
            ),
            (ids,),
        ).fetchall()
        records[entity] = [r[0] for r in got]
    for note in records.get("audio_note", []):
        try:
            note["_file"] = base64.b64encode(storage.read(note["uri"])).decode()
        except Exception as e:
            log.warning("voice note %s: no file to upload (%s)", note["id"], e)
    state = conn.execute("SELECT value FROM edge_state WHERE key = 'audit'").fetchone()
    after = int(state[0]["id"]) if state else 0
    entries = conn.execute(
        "SELECT id, ts, actor, action, entity, entity_id, payload, encode(prev_hash, 'hex'),"
        " encode(payload_hash, 'hex') FROM audit_log WHERE id > %s ORDER BY id LIMIT %s",
        (after, MAX_BATCH * 2),
    ).fetchall()
    keys = ("id", "ts", "actor", "action", "entity", "entity_id", "payload", "prev_hash")
    audit_rows = [
        {**dict(zip(keys, e[:8], strict=True)), "payload_hash": e[8], "ts": e[1].isoformat()}
        for e in entries
    ]
    feed = conn.execute(
        "SELECT row_to_json(f) FROM rt_feed f WHERE wellbore_id = %s ORDER BY updated_at DESC"
        " LIMIT 1",
        (wellbore_id,),
    ).fetchone()
    return {
        "edge_id": edge_id,
        "wellbore_id": wellbore_id,
        "records": records,
        "audit": audit_rows,
        "feed": feed[0] if feed else None,
    }, upto


def sent(conn: psycopg.Connection, upto: int, audit_head: dict[str, Any] | None) -> None:
    """After HQ accepted a batch: drop the outbox rows it covered, move the audit watermark."""
    with conn.transaction():
        conn.execute("DELETE FROM edge_outbox WHERE id <= %s", (upto,))
        if audit_head:
            conn.execute(
                "INSERT INTO edge_state VALUES ('audit', %s)"
                " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                (Jsonb(audit_head),),
            )


# ---------------------------------------------------------------- HQ: receive
def _upsert(
    conn: psycopg.Connection, edge_id: str, entity: str, row: dict[str, Any], cols: list[str]
) -> int:
    local = int(row["id"])
    mapped = conn.execute(
        "SELECT hq_id FROM edge_record WHERE edge_id = %s AND entity = %s AND local_id = %s",
        (edge_id, entity, local),
    ).fetchone()
    data = {k: v for k, v in row.items() if k != "id" and not k.startswith("_")}
    ident = sql.SQL(", ").join(map(sql.Identifier, cols))
    rec = sql.SQL("jsonb_populate_record(NULL::{}, %s)").format(sql.Identifier(entity))
    if mapped:
        conn.execute(
            sql.SQL("UPDATE {} t SET ({}) = (SELECT {} FROM {} r) WHERE t.id = %s").format(
                sql.Identifier(entity), ident, ident, rec
            ),
            (Jsonb(data), mapped[0]),
        )
        return int(mapped[0])
    got = conn.execute(
        sql.SQL("INSERT INTO {} ({}) SELECT {} FROM {} r RETURNING id").format(
            sql.Identifier(entity), ident, ident, rec
        ),
        (Jsonb(data),),
    ).fetchone()
    assert got is not None
    conn.execute(
        "INSERT INTO edge_record (edge_id, entity, local_id, hq_id) VALUES (%s, %s, %s, %s)",
        (edge_id, entity, local, got[0]),
    )
    return int(got[0])


class AuditEntry(BaseModel):
    id: int
    ts: datetime
    actor: str
    action: str
    entity: str | None = None
    entity_id: str | None = None
    payload: dict[str, Any]
    prev_hash: str = Field(pattern="^[0-9a-f]{64}$")
    payload_hash: str = Field(pattern="^[0-9a-f]{64}$")


class Batch(BaseModel):
    """What a rig box uploads (built by `batch`)."""

    edge_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    wellbore_id: int
    records: dict[Literal["alert_shelf", "alert", "audio_note"], list[dict[str, Any]]] = {}
    audit: list[AuditEntry] = Field(default=[], max_length=MAX_BATCH * 2)
    feed: dict[str, Any] | None = None


def receive(conn: psycopg.Connection, user: User, b: dict[str, Any]) -> dict[str, Any]:
    """Apply one batch (the caller's scoped transaction: RLS limits it to the account's blocks)."""
    try:
        b = Batch.model_validate(b).model_dump(mode="json")
    except ValidationError as err:
        raise HTTPException(422, err.errors(include_url=False, include_context=False)) from err
    if any(len(rows) > MAX_BATCH or any("id" not in r for r in rows)
           for rows in b["records"].values()):  # fmt: skip
        raise HTTPException(422, f"at most {MAX_BATCH} records per table, each with its id")
    edge_id = str(b["edge_id"])
    own = user.name.removeprefix("service-account-edge-sync-")
    if own != user.name and own != edge_id:  # a per-box client may only speak for its box
        raise HTTPException(403, f"this account syncs box {own}, not {edge_id}")
    conn.execute("INSERT INTO edge_box (edge_id) VALUES (%s) ON CONFLICT DO NOTHING", (edge_id,))
    box = conn.execute(
        "SELECT last_audit_id, last_audit_hash FROM edge_box WHERE edge_id = %s FOR UPDATE",
        (edge_id,),
    ).fetchone()
    assert box is not None
    entries = b.get("audit") or []
    prev = box[1]
    for e in entries:  # the rig's chain must continue from what HQ has, entry by entry
        if box[0] is not None and int(e["id"]) <= box[0]:
            continue  # already received (a re-sent batch)
        if prev is not None and e["prev_hash"] != prev:
            raise HTTPException(409, f"audit chain of {edge_id} breaks at rig entry {e['id']}")
        row = conn.execute(
            "SELECT encode(sha256(decode(%s, 'hex') || audit_canon(r)), 'hex')"
            " FROM jsonb_populate_record(NULL::audit_log, %s) r",
            (e["prev_hash"], Jsonb({k: v for k, v in e.items() if not k.endswith("_hash")})),
        ).fetchone()
        if row is None or row[0] != e["payload_hash"]:
            raise HTTPException(409, f"rig audit entry {e['id']} does not match its hash")
        prev = e["payload_hash"]
    new_entries = [e for e in entries if box[0] is None or int(e["id"]) > box[0]]

    counts: dict[str, int] = {}
    shelves: dict[int, int] = {}
    for entity in UPLOADED:
        cols = [c for c in _columns(conn, entity) if c != "id"]
        for row in (b.get("records") or {}).get(entity, []):
            if entity == "alert" and row.get("shelf_id"):
                row["shelf_id"] = shelves.get(row["shelf_id"]) or _mapped(
                    conn, edge_id, "alert_shelf", row["shelf_id"]
                )
            if entity == "audio_note" and row.get("_file"):
                ext = re.sub(r"[^a-z0-9]", "", str(row["uri"]).rsplit(".", 1)[-1].lower())[:5]
                name = re.sub(r"[^A-Za-z0-9_.-]", "_", str(row.get("client_id") or row["id"]))
                key = f"edge/{edge_id}/{name}.{ext or 'bin'}"
                storage.put(
                    "audio", key, base64.b64decode(row["_file"]), "application/octet-stream"
                )
                row["uri"] = f"s3://audio/{key}"
            hq = _upsert(conn, edge_id, entity, row, cols)
            if entity == "alert_shelf":
                shelves[int(row["id"])] = hq
            counts[entity] = counts.get(entity, 0) + 1
    feed = b.get("feed")
    if feed:
        conn.execute(
            "INSERT INTO rt_feed (wellbore_id, connector, config, state, last_sample_ts,"
            " last_rx_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, now())"
            " ON CONFLICT (wellbore_id, connector) DO UPDATE SET state = EXCLUDED.state,"
            " last_sample_ts = EXCLUDED.last_sample_ts, last_rx_at = EXCLUDED.last_rx_at,"
            " updated_at = now()",
            (
                feed["wellbore_id"],
                f"edge:{edge_id}:{feed['connector']}",
                Jsonb({"edge_id": edge_id}),
                feed["state"],
                feed["last_sample_ts"],
                feed["last_rx_at"],
            ),
        )
    head = new_entries[-1] if new_entries else None
    conn.execute(
        "UPDATE edge_box SET last_sync_at = now(), last_audit_id = coalesce(%s, last_audit_id),"
        " last_audit_hash = coalesce(%s, last_audit_hash) WHERE edge_id = %s",
        (head and head["id"], head and head["payload_hash"], edge_id),
    )
    audit.record(
        conn,
        user.name,
        "edge.sync",
        "edge_box",
        edge_id,
        {"counts": counts, "edge_audit": new_entries},
    )
    last = entries[-1] if entries else None
    return {
        "counts": counts,
        "audit_entries": len(new_entries),
        "audit_head": last and {"id": last["id"], "hash": last["payload_hash"]},
    }


def _mapped(conn: psycopg.Connection, edge_id: str, entity: str, local: int) -> int | None:
    row = conn.execute(
        "SELECT hq_id FROM edge_record WHERE edge_id = %s AND entity = %s AND local_id = %s",
        (edge_id, entity, local),
    ).fetchone()
    return int(row[0]) if row else None


@router.post("/sync")
def sync(
    body: Annotated[dict[str, Any], Body()],
    user: User = CurrentUser,
    conn: psycopg.Connection = Conn,
) -> dict[str, Any]:
    """A rig box's batch: alerts, shelves, voice notes (with files), feed state and its audit
    entries. Idempotent; 409 when the rig's audit chain does not continue."""
    user.require("sync")
    return receive(conn, user, body)


@router.get("/package/{wellbore_id}")
def package(
    wellbore_id: int,
    files: bool = Query(False),
    user: User = CurrentUser,
    conn: psycopg.Connection = Conn,
) -> Response:
    """The hazard package for a rig box, within the account's licences."""
    user.require("sync")
    owner = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)
    with owner:
        try:
            blocks, jvs = scope(owner, wellbore_id)
        except LookupError as e:
            raise HTTPException(404, str(e)) from e
        blocks = [x for x in blocks if x in user.blocks]
        jvs = [x for x in jvs if x in user.jvs]
        try:
            bank = Path(os.environ.get("RT_DETECTORS", "data/models/realtime/detectors.joblib"))
            data = pack(owner, wellbore_id, blocks, jvs, files=files, detectors=bank)
        except LookupError as e:
            raise HTTPException(404, str(e)) from e
    audit.record(
        conn, user.name, "edge.package", "wellbore", wellbore_id,
        {"blocks": blocks, "jvs": jvs, "files": files, "bytes": len(data)},
    )  # fmt: skip
    return Response(data, media_type="application/gzip")


# ---------------------------------------------------------------- the rig's agent
class Agent:
    """Uploads the outbox and audit entries when HQ answers; refreshes the package."""

    def __init__(self, dsn: str, wellbore_id: int, package_every_s: float = 6 * 3600) -> None:
        self.dsn, self.wellbore_id = dsn, wellbore_id
        self.hq = os.environ["HQ_URL"].rstrip("/")
        self.token_url = os.environ["HQ_TOKEN_URL"]
        self.edge_id = os.environ["EDGE_ID"]
        self.secret = os.environ["EDGE_SYNC_CLIENT_SECRET"]
        self.ctx = ssl.create_default_context(cafile=os.environ.get("HQ_CA_FILE"))
        self.package_every_s = package_every_s
        self.last_package = 0.0

    def _http(self, req: urllib.request.Request, timeout: float = 60) -> bytes:
        with urllib.request.urlopen(req, timeout=timeout, context=self.ctx) as r:
            return bytes(r.read())

    def link_up(self) -> bool:
        try:
            self._http(urllib.request.Request(f"{self.hq}/health"), timeout=5)
            return True
        except OSError:
            return False

    def token(self) -> str:
        body = urllib.parse.urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": os.environ.get("EDGE_SYNC_CLIENT_ID", "edge-sync"),
                "client_secret": self.secret,
            }
        ).encode()
        return str(
            json.loads(self._http(urllib.request.Request(self.token_url, body)))["access_token"]
        )

    def once(self) -> dict[str, Any]:
        if not self.link_up():
            return {"link": "down"}
        auth = {"Authorization": f"Bearer {self.token()}", "Content-Type": "application/json"}
        out: dict[str, Any] = {"link": "up", "uploaded": 0}
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            for _ in range(20):  # drain the backlog, a batch at a time (feed state each time)
                b, upto = batch(conn, self.edge_id, self.wellbore_id)
                req = urllib.request.Request(
                    f"{self.hq}/edge/sync", json.dumps(b, default=str).encode(), auth
                )
                res = json.loads(self._http(req))
                sent(conn, upto, res.get("audit_head"))
                out["uploaded"] += sum(res["counts"].values()) + res["audit_entries"]
                if not upto and len(b["audit"]) < MAX_BATCH * 2:
                    break
            if time.monotonic() - self.last_package > self.package_every_s:
                req = urllib.request.Request(
                    f"{self.hq}/edge/package/{self.wellbore_id}?files=false", headers=auth
                )
                models = os.environ.get("EDGE_MODELS_DIR")
                out["package"] = load(
                    conn, self._http(req, timeout=600), Path(models) if models else None
                )
                self.last_package = time.monotonic()
        return out

    def run(self, interval: float) -> None:
        while True:
            try:
                log.info("sync %s", json.dumps(self.once(), default=str))
            except Exception:  # keep going: the link comes and goes
                log.exception("sync failed; retrying in %.0f s", interval)
            time.sleep(interval)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    p = argparse.ArgumentParser(prog="python -m api.edge")
    sub = p.add_subparsers(dest="cmd", required=True)
    pk = sub.add_parser("pack", help="write a hazard package to stdout")
    pk.add_argument("--wellbore-id", type=int, required=True)
    pk.add_argument("--no-files", action="store_true")
    pk.add_argument("--detectors", type=Path, default=Path(os.environ.get("RT_DETECTORS", "")))
    ld = sub.add_parser("load", help="load a hazard package into this database")
    ld.add_argument("file", type=Path)
    ld.add_argument("--models-dir", type=Path)
    ag = sub.add_parser("agent", help="sync with headquarters whenever it answers")
    ag.add_argument("--interval", type=float, default=60)
    ag.add_argument("--package-every", type=float, default=6 * 3600)
    a = p.parse_args()
    dsn = os.environ["DATABASE_URL"]
    if a.cmd == "agent":
        Agent(dsn, int(os.environ["EDGE_WELLBORE_ID"]), a.package_every).run(a.interval)
        return
    with psycopg.connect(dsn, autocommit=True) as conn:
        if a.cmd == "pack":
            blocks, jvs = scope(conn, a.wellbore_id)
            det = a.detectors if str(a.detectors) not in ("", ".") else None
            sys.stdout.buffer.write(pack(conn, a.wellbore_id, blocks, jvs, not a.no_files, det))
        else:
            print(json.dumps(load(conn, a.file.read_bytes(), a.models_dir), indent=1))


if __name__ == "__main__":
    main()
