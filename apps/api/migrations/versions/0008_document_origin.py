"""Where a document or audio note came from, and whether it is synthetic.

Revision ID: 0008
Revises: 0007
"""

from alembic import op

revision = "0008"
down_revision = "0007"


def run(sql: str) -> None:
    # Raw driver call: multi-statement SQL, no bind-parameter parsing of ':' or '%'.
    op.get_bind().connection.driver_connection.execute(sql)


def upgrade() -> None:
    run("""
    ALTER TABLE document
        ADD COLUMN source text NOT NULL DEFAULT 'upload',
        ADD COLUMN synthetic boolean NOT NULL DEFAULT false;
    ALTER TABLE audio_note
        ADD COLUMN source text NOT NULL DEFAULT 'upload',
        ADD COLUMN synthetic boolean NOT NULL DEFAULT false;
    COMMENT ON COLUMN document.source IS
        'Origin: upload, sodir, volve, forge, synthetic_assam, ...';
    COMMENT ON COLUMN document.synthetic IS
        'Generated test data, not a real record. Answers citing it must say so.';
    COMMENT ON COLUMN audio_note.synthetic IS
        'Generated test data (synthetic voice), not a real recording.';
    CREATE INDEX ON document (source);
    """)


def downgrade() -> None:
    run("""
    ALTER TABLE audio_note DROP COLUMN synthetic, DROP COLUMN source;
    ALTER TABLE document DROP COLUMN synthetic, DROP COLUMN source;
    """)
