"""Seed loads, and the example queries in docs/data-model.md return what the docs say."""

import psycopg
import pytest

from api import seed
from api.db import scoped

# Keep in sync with docs/data-model.md "Example queries".
OFFSET_LOSSES_AT_BARAIL_TOP = """
WITH ref AS (
    SELECT position FROM chat.v_tops
    WHERE well = 'DEMO-1' AND wellbore = 'DEMO-1' AND formation = 'Barail'
)
SELECT t.well,
       round(ST_Distance(t.position::geography, ref.position::geography)) AS distance_m,
       e.loss_bbl, e.md_top, e.source_document, e.source_page
FROM ref
JOIN chat.v_tops t ON t.formation = 'Barail' AND t.well <> 'DEMO-1'
                  AND ST_DWithin(t.position::geography, ref.position::geography, 5000)
JOIN chat.v_events e ON e.wellbore_id = t.wellbore_id AND e.formation = 'Barail'
WHERE e.event_type = 'loss' AND e.md_top - t.md BETWEEN 0 AND 50
ORDER BY distance_m
"""

LOSSES_BY_FORMATION = """
SELECT formation, count(*) AS events, sum(npt_hr) AS npt_hr, sum(loss_bbl) AS loss_bbl
FROM chat.v_events WHERE event_type = 'loss'
GROUP BY formation ORDER BY loss_bbl DESC
"""

ECD_MARGIN = """
SELECT e.well, e.ecd, c.shoe_test, c.shoe_test_emw,
       round((c.shoe_test_emw - e.ecd)::numeric, 2) AS margin_sg
FROM chat.v_events e
JOIN chat.v_casing c ON c.wellbore_id = e.wellbore_id AND c.kind = 'intermediate'
WHERE e.event_type = 'loss'
ORDER BY margin_sg
"""

FULL_TEXT = """
SELECT d.title, r.page, ts_rank(c.tsv, q) AS rank
FROM chunk c
JOIN source_ref r ON r.id = c.source_ref_id
JOIN document d ON d.id = r.document_id,
     websearch_to_tsquery('english', 'lost returns Barail') q
WHERE c.tsv @@ q
ORDER BY rank DESC
"""


@pytest.fixture(scope="module")
def seeded(db_url: str) -> str:
    mp = pytest.MonkeyPatch()
    mp.setenv("DATABASE_URL", db_url)
    seed.main()
    seed.main()  # idempotent: second run is a no-op
    mp.undo()
    return db_url


ALL = ["AA-ONN-2001/1", "AA-ONN-2001/2"]


def test_counts(conn: psycopg.Connection, seeded: str) -> None:
    def n(sql: str) -> int:
        row = conn.execute(sql).fetchone()
        assert row is not None
        return int(row[0])

    assert n("SELECT count(*) FROM formation") == 3
    assert n("SELECT count(*) FROM well") == 5
    assert n("SELECT count(*) FROM wellbore WHERE parent_id IS NOT NULL") == 1
    assert n("SELECT count(*) FROM trajectory") == 6  # one per wellbore, built by trigger
    assert n("SELECT count(*) FROM event") == 6
    assert conn.execute("SELECT audit_verify()").fetchone() == (None,)


def test_trajectories_follow_surveys(conn: psycopg.Connection, seeded: str) -> None:
    rows = conn.execute(
        "SELECT count(*) FROM trajectory t JOIN wellbore b ON b.id = t.wellbore_id"
        " WHERE ST_NPoints(t.geom) <> (SELECT count(*) FROM survey_station s"
        "                              WHERE s.wellbore_id = t.wellbore_id)"
    ).fetchone()
    assert rows == (0,)


def test_offset_losses_at_barail_top(conn: psycopg.Connection, seeded: str) -> None:
    with scoped(conn, "app_ro", blocks=ALL):
        rows = conn.execute(OFFSET_LOSSES_AT_BARAIL_TOP).fetchall()
    assert [r[0] for r in rows] == ["DEMO-2"]
    assert 3000 < rows[0][1] < 5000


def test_offset_query_respects_rls(conn: psycopg.Connection, seeded: str) -> None:
    # DEMO-1 is in block /1. A user with only block /2 cannot even find the reference well.
    with scoped(conn, "app_ro", blocks=["AA-ONN-2001/2"]):
        assert conn.execute(OFFSET_LOSSES_AT_BARAIL_TOP).fetchall() == []


def test_other_examples_run(conn: psycopg.Connection, seeded: str) -> None:
    with scoped(conn, "app_ro", blocks=ALL):
        by_formation = conn.execute(LOSSES_BY_FORMATION).fetchall()
        assert by_formation[0][:2] == ("Barail", 3)
        margins = conn.execute(ECD_MARGIN).fetchall()
        assert len(margins) == 3 and all(r[4] > 0 for r in margins)
    with scoped(conn, "app_rw", blocks=ALL):
        hits = conn.execute(FULL_TEXT).fetchall()
        assert len(hits) >= 3
