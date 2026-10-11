"""Roles, grants and row-level security (ABAC by block and JV).

Revision ID: 0006
Revises: 0005
"""

from alembic import op

revision = "0006"
down_revision = "0005"

# Tables whose rows carry their own block_id / jv_id.
SCOPED = ["well", "document", "audio_note", "lesson"]
# Tables scoped through their wellbore.
BY_WELLBORE = [
    "survey_station", "trajectory", "formation_top", "event", "casing_string", "cement_job",
    "mud_record", "bit_run", "lot_fit", "rt_measurement", "hazard_profile", "alert",
]  # fmt: skip
# Scoped through a parent row: (table, predicate).
BY_PARENT = [
    ("wellbore", "well_id IN (SELECT id FROM well)"),
    ("mitigation", "event_id IN (SELECT id FROM event)"),
    ("chunk", "source_ref_id IN (SELECT id FROM source_ref)"),
    (
        "source_ref",
        "document_id IN (SELECT id FROM document) OR audio_note_id IN (SELECT id FROM audio_note)",
    ),
]
RLS_TABLES = SCOPED + BY_WELLBORE + [t for t, _ in BY_PARENT]
# Shared reference data and the knowledge graph: readable by the app, no row filter.
UNSCOPED = ["formation", "edge"]


def run(sql: str) -> None:
    # Raw driver call: multi-statement SQL, no bind-parameter parsing of ':' or '%'.
    op.get_bind().connection.driver_connection.execute(sql)


def upgrade() -> None:
    run("""
    -- Cluster-wide roles; may already exist from another database in the cluster.
    DO $$ BEGIN
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'app_rw') THEN
            CREATE ROLE app_rw NOLOGIN;
        END IF;
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'app_ro') THEN
            CREATE ROLE app_ro NOLOGIN;
        END IF;
    END $$;
    COMMENT ON ROLE app_rw IS 'API and user-driven writes. Subject to RLS.';
    COMMENT ON ROLE app_ro IS 'Chat text-to-SQL. SELECT on chat.* views only.';

    GRANT SELECT ON block, jv TO app_rw;
    GRANT INSERT ON audit_log TO app_rw;
    """)
    rw = ", ".join(RLS_TABLES + UNSCOPED)
    run(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {rw} TO app_rw;")

    for t in RLS_TABLES:
        run(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;")
    for t in SCOPED:
        run(f"CREATE POLICY scope ON {t} USING (app_visible(block_id, jv_id));")
    for t in BY_WELLBORE:
        run(f"CREATE POLICY scope ON {t} USING (wellbore_id IN (SELECT id FROM wellbore));")
    for t, predicate in BY_PARENT:
        run(f"CREATE POLICY scope ON {t} USING ({predicate});")


def downgrade() -> None:
    for t in RLS_TABLES:
        run(f"DROP POLICY scope ON {t}; ALTER TABLE {t} DISABLE ROW LEVEL SECURITY;")
    # Roles are cluster-wide: drop only this database's grants, then the role if nothing else
    # in the cluster still depends on it.
    run("""
    DROP OWNED BY app_rw, app_ro;
    DO $$ BEGIN
        DROP ROLE app_rw, app_ro;
    EXCEPTION WHEN dependent_objects_still_exist THEN
        RAISE NOTICE 'roles still used by another database; kept';
    END $$;
    """)
