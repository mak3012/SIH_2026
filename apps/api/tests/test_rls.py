"""RLS: a user granted block A never reads or writes block B, through tables or chat views."""

import psycopg
import pytest

from api.db import scoped

A, B, JV = "T-A", "T-B", "T-JV"


@pytest.fixture(scope="module")
def two_blocks(db_url: str) -> dict[str, int]:
    """One well per block, each with a wellbore, event, document, source_ref and chunk.
    Well B also belongs to JV T-JV."""
    ids: dict[str, int] = {}
    with psycopg.connect(db_url) as c:
        c.execute("INSERT INTO block VALUES (%s, 'A', 'X'), (%s, 'B', 'X')", (A, B))
        c.execute("INSERT INTO jv VALUES (%s, 'JV', 'Op')", (JV,))
        c.execute("INSERT INTO basin (id, name) VALUES ('X', 'X') ON CONFLICT DO NOTHING")
        f = c.execute("INSERT INTO formation (basin, name) VALUES ('X', 'F') RETURNING id")
        fid = f.fetchone()[0]
        for tag, block, jv in (("a", A, None), ("b", B, JV)):

            def one(sql: str, *params: object) -> int:
                return c.execute(sql + " RETURNING id", params).fetchone()[0]

            doc = one(
                "INSERT INTO document (sha256, type, title, uri, block_id, jv_id)"
                " VALUES (sha256(%s::bytea), 'ddr', %s, 's3://x', %s, %s)",
                tag.encode(), f"DDR {tag}", block, jv,
            )  # fmt: skip
            src = one(
                "INSERT INTO source_ref (document_id, page, extractor_version) VALUES (%s, 1, 't')",
                doc,
            )
            well = one(
                "INSERT INTO well (name, block_id, jv_id, surface, rt_elevation_m)"
                " VALUES (%s, %s, %s, ST_SetSRID(ST_MakePoint(95, 27), 4326), 100)",
                f"W-{tag}", block, jv,
            )  # fmt: skip
            wb = one("INSERT INTO wellbore (well_id, name) VALUES (%s, %s)", well, f"W-{tag}")
            one(
                "INSERT INTO event (wellbore_id, type, formation_id, source_ref_id)"
                " VALUES (%s, 'loss', %s, %s)",
                wb, fid, src,
            )  # fmt: skip
            one("INSERT INTO chunk (source_ref_id, text) VALUES (%s, 'lost returns')", src)
            ids |= {f"wellbore_{tag}": wb, f"source_{tag}": src}
    return ids


def names(conn: psycopg.Connection, sql: str) -> set[str]:
    return {r[0] for r in conn.execute(sql)}


def test_block_a_sees_only_block_a(conn: psycopg.Connection, two_blocks: dict[str, int]) -> None:
    with scoped(conn, "app_rw", blocks=[A]):
        assert names(conn, "SELECT name FROM well") == {"W-a"}
        assert names(conn, "SELECT name FROM wellbore") == {"W-a"}
        assert names(conn, "SELECT title FROM document") == {"DDR a"}
        rows = conn.execute("SELECT wellbore_id FROM event").fetchall()
        assert rows == [(two_blocks["wellbore_a"],)]
        rows = conn.execute("SELECT source_ref_id FROM chunk").fetchall()
        assert rows == [(two_blocks["source_a"],)]


def test_cross_block_write_is_refused(conn: psycopg.Connection, two_blocks: dict[str, int]) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="row-level security"):
        with scoped(conn, "app_rw", blocks=[A]):
            conn.execute(
                "INSERT INTO event (wellbore_id, type, source_ref_id) VALUES (%s, 'kick', %s)",
                (two_blocks["wellbore_b"], two_blocks["source_a"]),
            )
    with scoped(conn, "app_rw", blocks=[A]):
        # Rows B cannot be seen cannot be changed either: the update matches nothing.
        cur = conn.execute("UPDATE well SET status = 'x' WHERE name = 'W-b'")
        assert cur.rowcount == 0


def test_no_attributes_means_no_rows(conn: psycopg.Connection, two_blocks: dict[str, int]) -> None:
    with scoped(conn, "app_rw", blocks=[]):
        assert names(conn, "SELECT name FROM well") == set()
        assert names(conn, "SELECT text FROM chunk") == set()


def test_jv_partner_sees_jv_rows(conn: psycopg.Connection, two_blocks: dict[str, int]) -> None:
    with scoped(conn, "app_rw", blocks=[], jvs=[JV]):
        assert names(conn, "SELECT name FROM well") == {"W-b"}


def test_chat_views_are_filtered_and_base_tables_closed(
    conn: psycopg.Connection, two_blocks: dict[str, int]
) -> None:
    with scoped(conn, "app_ro", blocks=[A]):
        assert names(conn, "SELECT well FROM chat.v_wells") == {"W-a"}
        assert names(conn, "SELECT well FROM chat.v_events") == {"W-a"}
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with scoped(conn, "app_ro", blocks=[A, B]):
            conn.execute("SELECT * FROM well")
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        with scoped(conn, "app_ro", blocks=[A]):
            conn.execute("CREATE TEMP TABLE t (x int)")


def test_scoped_refuses_to_nest(conn: psycopg.Connection) -> None:
    conn.autocommit = False
    conn.execute("SELECT 1")
    with pytest.raises(RuntimeError):
        with scoped(conn, "app_rw", blocks=[A]):
            pass
    conn.rollback()
    conn.autocommit = True
