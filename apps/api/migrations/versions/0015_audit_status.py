"""Audit chain status for the app role: entries, head and the first broken id.

The app role may append to audit_log but not read it; the admin page and the anchor job need
the head and the verification result, so they get them through this SECURITY DEFINER function
and nothing else. An index on (entity, entity_id) serves "who touched this record" queries.

Revision ID: 0015
Revises: 0014
"""

from alembic import op

revision = "0015"
down_revision = "0014"


def run(sql: str) -> None:
    op.get_bind().connection.driver_connection.execute(sql)


def upgrade() -> None:
    run("""
    CREATE FUNCTION audit_status(
        OUT entries bigint, OUT head_id bigint, OUT head_hash text, OUT first_broken bigint)
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
        SELECT (SELECT count(*) FROM audit_log),
               (SELECT max(id) FROM audit_log),
               (SELECT encode(payload_hash, 'hex') FROM audit_log ORDER BY id DESC LIMIT 1),
               audit_verify()
    $$;
    REVOKE ALL ON FUNCTION audit_status() FROM PUBLIC;
    GRANT EXECUTE ON FUNCTION audit_status() TO app_rw;
    CREATE INDEX audit_log_entity ON audit_log (entity, entity_id);
    """)


def downgrade() -> None:
    run("DROP INDEX audit_log_entity; DROP FUNCTION audit_status();")
