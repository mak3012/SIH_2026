"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
"""

from alembic import op

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}


def run(sql: str) -> None:
    # Raw driver call: multi-statement SQL, no bind-parameter parsing of ':' or '%'.
    op.get_bind().connection.driver_connection.execute(sql)


def upgrade() -> None:
    run("""
    """)


def downgrade() -> None:
    run("""
    """)
