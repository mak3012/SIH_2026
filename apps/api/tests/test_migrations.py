import psycopg
from alembic import command
from alembic.config import Config

TABLES = {
    "block", "jv", "document", "audio_note", "source_ref", "chunk", "formation", "well",
    "wellbore", "survey_station", "trajectory", "formation_top", "event", "mitigation",
    "casing_string", "cement_job", "mud_record", "bit_run", "lot_fit", "rt_measurement",
    "hazard_profile", "alert", "lesson", "edge", "audit_log",
}  # fmt: skip
VIEWS = {"v_wells", "v_events", "v_tops", "v_casing"}


def _state(url: str) -> tuple[set[str], set[str], int]:
    with psycopg.connect(url) as c:
        tables = {r[0] for r in c.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            " AND tablename NOT IN ('spatial_ref_sys', 'alembic_version')")}  # fmt: skip
        views = {r[0] for r in c.execute(
            "SELECT viewname FROM pg_views WHERE schemaname = 'chat'")}  # fmt: skip
        row = c.execute(
            "SELECT count(*) FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace"
            " WHERE n.nspname = 'public' AND t.typtype = 'e'"
        ).fetchone()
        assert row is not None
        return tables, views, row[0]


def test_up_down_up(db_url: str, cfg: Config) -> None:
    tables, views, enums = _state(db_url)
    assert TABLES <= tables and views == VIEWS and enums > 0

    command.downgrade(cfg, "base")
    assert _state(db_url) == (set(), set(), 0)

    command.upgrade(cfg, "head")
    assert _state(db_url) == (tables, views, enums)


def test_every_step_downgrades(cfg: Config) -> None:
    for rev in ["0009", "0008", "0007", "0006", "0005", "0004", "0003", "0002", "0001", "base"]:
        command.downgrade(cfg, rev)
    command.upgrade(cfg, "head")


def test_rt_measurement_is_hypertable(conn: psycopg.Connection) -> None:
    row = conn.execute(
        "SELECT count(*) FROM timescaledb_information.hypertables"
        " WHERE hypertable_name = 'rt_measurement'"
    ).fetchone()
    assert row == (1,)


def test_every_chat_column_is_commented(conn: psycopg.Connection) -> None:
    missing = conn.execute(
        "SELECT c.relname, a.attname FROM pg_attribute a"
        " JOIN pg_class c ON c.oid = a.attrelid"
        " JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = 'chat' AND a.attnum > 0 AND NOT a.attisdropped"
        " AND col_description(c.oid, a.attnum) IS NULL"
    ).fetchall()
    assert missing == []
