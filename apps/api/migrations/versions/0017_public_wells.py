"""Public wells: real wellbores from open registers (Sodir FactPages), synced daily.

Kept apart from `well`/`wellbore` (the wells this system drills and analyses): a public
register lists thousands of wellbores with a location and a few headline facts, no surveys or
events. The offset map draws them as their own layer; their reports are registered as
documents and go through the same extraction pipeline.

Revision ID: 0017
Revises: 0016
"""

from alembic import op

revision = "0017"
down_revision = "0016"


def run(sql: str) -> None:
    op.get_bind().connection.driver_connection.execute(sql)


def upgrade() -> None:
    run("""
    INSERT INTO block (id, name, basin) VALUES
        ('NO-SODIR', 'Norwegian Continental Shelf (Sodir public)', 'Norwegian Continental Shelf')
    ON CONFLICT (id) DO NOTHING;

    CREATE TABLE public_well (
        id                 bigint PRIMARY KEY,
        source             text NOT NULL,
        name               text NOT NULL,
        block_id           text NOT NULL REFERENCES block,
        jv_id              text REFERENCES jv,
        surface            geometry(Point, 4326) NOT NULL,
        area               text,
        purpose            text,
        status             text,
        content            text,
        operator           text,
        licence            text,
        field              text,
        entry_date         date,
        completion_date    date,
        total_depth_m      real,
        water_depth_m      real,
        formation_at_td    text,
        factpage_url       text,
        press_release_url  text,
        source_updated     date,
        first_seen         timestamptz NOT NULL DEFAULT now(),
        synced_at          timestamptz NOT NULL DEFAULT now(),
        reports_checked_at timestamptz,
        UNIQUE (source, name)
    );
    COMMENT ON TABLE public_well IS
        'Wellbores from public registers (id = the register''s id, e.g. Sodir NPDID). '
        'first_seen = when this system first saw it: the map marks recent ones as new.';
    CREATE INDEX ON public_well USING gist (surface);
    CREATE INDEX ON public_well (entry_date DESC);

    ALTER TABLE public_well ENABLE ROW LEVEL SECURITY;
    CREATE POLICY scope ON public_well USING (app_visible(block_id, jv_id));
    GRANT SELECT ON public_well TO app_rw;
    """)


def downgrade() -> None:
    run("DROP TABLE public_well;")
