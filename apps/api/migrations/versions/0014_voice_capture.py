"""Voice notes captured in the app: who recorded them, for which well and formation.

Pipeline-registered notes leave these NULL. A captured note is pending expert confirmation until
a lesson from it is approved (lesson.status) or its review items are resolved.

Revision ID: 0014
Revises: 0013
"""

from alembic import op

revision = "0014"
down_revision = "0013"


def run(sql: str) -> None:
    op.get_bind().connection.driver_connection.execute(sql)


def upgrade() -> None:
    run("""
    ALTER TABLE audio_note
        ADD COLUMN wellbore_id  bigint REFERENCES wellbore ON DELETE SET NULL,
        ADD COLUMN formation_id bigint REFERENCES formation,
        ADD COLUMN note         text,
        ADD COLUMN uploaded_by  text,
        ADD COLUMN client_id    text UNIQUE;
    COMMENT ON COLUMN audio_note.client_id IS
        'Id the capturing device gave the recording; makes offline re-uploads idempotent.';
    """)


def downgrade() -> None:
    run("""
    ALTER TABLE audio_note DROP COLUMN client_id, DROP COLUMN uploaded_by, DROP COLUMN note,
        DROP COLUMN formation_id, DROP COLUMN wellbore_id;
    """)
