"""Rig Edge Pack end to end against two databases: headquarters (the test database, demo seed
plus a planned well) and a rig box (a copy of its schema, emptied). Package down, records and
audit entries up, idempotent re-send, and a tampered rig audit entry refused."""

import copy
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from psycopg import sql
from test_intelligence_api import B1, headers, world  # noqa: F401

from api import audit, edge


@pytest.fixture(scope="module")
def rig(db_url: str, world: dict[str, Any]) -> Iterator[str]:  # noqa: F811
    """A rig database: HQ's schema (and test stand-ins), no rows."""
    u = urlsplit(db_url)
    name = u.path.lstrip("/") + "_rig"
    admin = urlunsplit(u._replace(path="/postgres"))
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        # A template must have no other sessions (earlier modules may leave idle ones behind).
        c.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
            " WHERE datname = %s AND pid <> pg_backend_pid()",
            (u.path.lstrip("/"),),
        )
        c.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
                sql.Identifier(name), sql.Identifier(u.path.lstrip("/"))
            )
        )
    url = urlunsplit(u._replace(path=f"/{name}"))
    with psycopg.connect(url, autocommit=True) as c:
        tables = [
            r[0]
            for r in c.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                " AND tablename NOT IN ('alembic_version', 'spatial_ref_sys')"
            )
        ]
        c.execute("ALTER TABLE audit_log DISABLE TRIGGER audit_no_truncate")
        c.execute(
            sql.SQL("TRUNCATE {} CASCADE").format(sql.SQL(", ").join(map(sql.Identifier, tables)))
        )
        c.execute("ALTER TABLE audit_log ENABLE TRIGGER audit_no_truncate")
    yield url
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


def test_package_round_trip_and_sync(db_url: str, rig: str, world: dict[str, Any]) -> None:  # noqa: F811
    bore = world["bore"]
    with psycopg.connect(db_url, autocommit=True) as hq:
        blocks, jvs = edge.scope(hq, bore)
        assert blocks == [B1]
        data = edge.pack(hq, bore, blocks, jvs, files=False)
        tops = hq.execute(
            "SELECT count(*) FROM formation_top t JOIN wellbore b ON b.id = t.wellbore_id"
            " JOIN well w ON w.id = b.well_id WHERE w.block_id = %s",
            (B1,),
        ).fetchone()
        other = hq.execute("SELECT count(*) FROM well WHERE block_id <> %s", (B1,)).fetchone()
    assert tops and other and other[0] > 0  # there is something the package must leave out

    with psycopg.connect(rig, autocommit=True) as r:
        out = edge.load(r, data)
        assert out["tables"]["wellbore"] >= 1
        n = r.execute("SELECT count(*) FROM formation_top").fetchone()
        assert n is not None and n[0] == tops[0]
        assert r.execute("SELECT count(*) FROM well WHERE block_id <> %s", (B1,)).fetchone() == (0,)
        assert edge.load(r, data)["tables"] == out["tables"]  # reloading upserts, no duplicates

        # The rig raises an alert, shelves a hazard and audits it: rig-range ids, queued.
        shelf = r.execute(
            "INSERT INTO alert_shelf (wellbore_id, hazard, reason, shelved_by, expires_at)"
            " VALUES (%s, 'kick', 'pit transfer', 'driller', now() + interval '1 hour')"
            " RETURNING id",
            (bore,),
        ).fetchone()
        aid = r.execute(
            "INSERT INTO alert (wellbore_id, raised_at, hazard, rule_id, rule_version, tier,"
            " prior, lr, posterior, evidence, shelf_id) VALUES (%s, now(), 'loss', 'RT-LOSS',"
            " 'realtime@1.0.0', 2, 0.1, 9, 0.5, '{}', %s) RETURNING id",
            (bore, shelf and shelf[0]),
        ).fetchone()
        assert shelf and aid and aid[0] >= edge.EDGE_ID_BASE and shelf[0] >= edge.EDGE_ID_BASE
        with r.transaction():
            audit.record(r, "driller", "alert.ack", "alert", aid[0], {"note": "seen"})
        b, upto = edge.batch(r, "rig-07", bore)
        assert [x["id"] for x in b["records"]["alert"]] == [aid[0]] and b["audit"]

    client = world["client"]
    sync = headers(roles="edge_sync", blocks=B1)
    res = client.post("/edge/sync", json=b, headers=sync)
    assert res.status_code == 200, res.text
    assert res.json()["counts"] == {"alert_shelf": 1, "alert": 1}
    again = client.post("/edge/sync", json=b, headers=sync)
    assert again.status_code == 200 and again.json()["audit_entries"] == 0
    with psycopg.connect(db_url) as hq:
        rows = hq.execute(
            "SELECT a.id, a.shelf_id = s.hq_id FROM alert a JOIN edge_record e ON e.hq_id = a.id"
            " AND e.entity = 'alert' JOIN edge_record s ON s.entity = 'alert_shelf'"
            " AND s.edge_id = 'rig-07' WHERE e.edge_id = 'rig-07'"
        ).fetchall()
        assert len(rows) == 1 and rows[0][0] < edge.EDGE_ID_BASE and rows[0][1]
        logged = hq.execute(
            "SELECT payload->'edge_audit' FROM audit_log WHERE action = 'edge.sync'"
            " ORDER BY id DESC LIMIT 2"
        ).fetchall()
        assert [len(x[0]) for x in logged] == [0, len(b["audit"])]

    # Only the edge service account may sync; an edited rig audit entry is refused.
    assert client.post("/edge/sync", json=b, headers=headers()).status_code == 403
    with psycopg.connect(rig, autocommit=True) as r:
        edge.sent(r, upto, res.json()["audit_head"])
        assert r.execute("SELECT count(*) FROM edge_outbox").fetchone() == (0,)
        with r.transaction():
            audit.record(r, "driller", "alert.shelve", "alert_shelf", shelf[0], {})
        b2, _ = edge.batch(r, "rig-07", bore)
    bad = copy.deepcopy(b2)
    bad["audit"][0]["actor"] = "someone else"
    refused = client.post("/edge/sync", json=bad, headers=sync)
    assert refused.status_code == 409 and "hash" in refused.json()["detail"]
    assert client.post("/edge/sync", json=b2, headers=sync).status_code == 200


def test_package_endpoint_within_the_account_licences(world: dict[str, Any]) -> None:  # noqa: F811
    import io
    import json
    import tarfile

    client, bore = world["client"], world["bore"]
    assert client.get(f"/edge/package/{bore}", headers=headers()).status_code == 403
    r = client.get(f"/edge/package/{bore}", headers=headers(roles="edge_sync", blocks=B1))
    assert r.status_code == 200 and r.headers["content-type"] == "application/gzip"
    with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as tar:
        m = json.loads(tar.extractfile("manifest.json").read())  # type: ignore[union-attr]
    assert m["blocks"] == [B1] and m["files"] == {} and "well" in m["tables"]
    none = client.get(f"/edge/package/{bore}", headers=headers(roles="edge_sync", blocks="X"))
    assert none.status_code == 404


def test_malformed_batches_and_packages_are_refused(rig: str, world: dict[str, Any]) -> None:  # noqa: F811
    import io
    import json
    import tarfile

    sync = headers(roles="edge_sync", blocks=B1)
    for bad in (
        {"edge_id": "../rig", "wellbore_id": 1},
        {"edge_id": "rig-1", "wellbore_id": 1, "records": {"audit_log": []}},
        {"edge_id": "rig-1", "wellbore_id": 1, "audit": [{"id": 1}]},
    ):
        assert world["client"].post("/edge/sync", json=bad, headers=sync).status_code == 422
    # A package may not load tables outside the package set (here: the audit log).
    with psycopg.connect(rig, autocommit=True) as r:
        manifest = {
            "format": edge.FORMAT, "alembic": edge._head(r), "created_at": "x", "files": {},
            "tables": {"audit_log": ["actor"]}, "sha256": {},
        }  # fmt: skip
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            data = json.dumps(manifest).encode()
            info = tarfile.TarInfo("manifest.json")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        with pytest.raises(ValueError, match="not one a package may load"):
            edge.load(r, buf.getvalue())
