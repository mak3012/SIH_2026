"""Rig Edge Pack: outbox on the rig box, idempotent receipt at headquarters (api/edge.py).

Rig box (after `python -m api.edge load`, which calls edge_enable()):
- identity sequences jump to EDGE_ID_BASE, so rows made on the rig never collide with rows a
  hazard package brings from headquarters (those keep HQ's ids);
- triggers queue every new or changed alert, shelf and voice note in edge_outbox;
- edge_state holds the loaded package's manifest and the last audit entry sent.

Headquarters:
- edge_box: one row per rig box, with the head of its audit chain as last received (the next
  batch must continue it, so a gap or an edited entry on the rig is refused);
- edge_record: (box, table, rig id) -> HQ id, so a re-sent batch updates instead of duplicating.

Revision ID: 0016
Revises: 0015
"""

from alembic import op

revision = "0016"
down_revision = "0015"


def run(sql: str) -> None:
    op.get_bind().connection.driver_connection.execute(sql)


def upgrade() -> None:
    run("""
    -- ---------------------------------------------------------------- rig box
    CREATE TABLE edge_outbox (
        id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        entity    text NOT NULL,
        entity_id bigint NOT NULL,
        queued_at timestamptz NOT NULL DEFAULT now()
    );
    COMMENT ON TABLE edge_outbox IS
        'Rig box: rows to upload to headquarters (filled by triggers after edge_enable()).';

    CREATE TABLE edge_state (
        key   text PRIMARY KEY,
        value jsonb NOT NULL
    );
    COMMENT ON TABLE edge_state IS
        'Rig box: package manifest (key package) and audit upload watermark (key audit).';

    CREATE FUNCTION edge_outbox_push() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        INSERT INTO edge_outbox (entity, entity_id) VALUES (TG_TABLE_NAME, NEW.id);
        RETURN NULL;
    END $$;

    -- Rig ids start here (2^50: far above HQ ids, below JavaScript's 2^53).
    CREATE FUNCTION edge_enable() RETURNS void LANGUAGE plpgsql AS $$
    DECLARE
        s record;
        base constant bigint := 1125899906842624;
    BEGIN
        FOR s IN SELECT pg_get_serial_sequence(format('%I', c.table_name), c.column_name) AS seq
                 FROM information_schema.columns c
                 WHERE c.table_schema = 'public' AND c.is_identity = 'YES'
                   AND c.table_name NOT IN ('edge_outbox', 'audit_log') LOOP
            IF s.seq IS NOT NULL THEN
                PERFORM setval(s.seq, greatest(base, nextval(s.seq)));
            END IF;
        END LOOP;
        DROP TRIGGER IF EXISTS edge_outbox ON alert;
        DROP TRIGGER IF EXISTS edge_outbox_update ON alert;
        DROP TRIGGER IF EXISTS edge_outbox ON alert_shelf;
        DROP TRIGGER IF EXISTS edge_outbox ON audio_note;
        CREATE TRIGGER edge_outbox AFTER INSERT ON alert
            FOR EACH ROW EXECUTE FUNCTION edge_outbox_push();
        -- Not on every sample's last_seen_at / peak_posterior update: only what changes the alert.
        CREATE TRIGGER edge_outbox_update AFTER UPDATE ON alert FOR EACH ROW
            WHEN ((OLD.status, OLD.tier, OLD.cleared_at, OLD.ack_at, OLD.feedback,
                   OLD.explanation_status, OLD.shelf_id, OLD.suppressed)
                  IS DISTINCT FROM
                  (NEW.status, NEW.tier, NEW.cleared_at, NEW.ack_at, NEW.feedback,
                   NEW.explanation_status, NEW.shelf_id, NEW.suppressed))
            EXECUTE FUNCTION edge_outbox_push();
        CREATE TRIGGER edge_outbox AFTER INSERT OR UPDATE ON alert_shelf
            FOR EACH ROW EXECUTE FUNCTION edge_outbox_push();
        CREATE TRIGGER edge_outbox AFTER INSERT OR UPDATE ON audio_note
            FOR EACH ROW WHEN (NEW.id >= 1125899906842624) EXECUTE FUNCTION edge_outbox_push();
    END $$;
    COMMENT ON FUNCTION edge_enable() IS
        'Rig box only: move identity sequences to the rig range and start the outbox triggers.';

    -- ---------------------------------------------------------------- headquarters
    CREATE TABLE edge_box (
        edge_id          text PRIMARY KEY,
        last_audit_id    bigint,
        last_audit_hash  text,
        last_sync_at     timestamptz,
        last_package_at  timestamptz
    );
    COMMENT ON TABLE edge_box IS
        'HQ: rig boxes and the head of each one''s audit chain as last received.';

    CREATE TABLE edge_record (
        edge_id   text NOT NULL REFERENCES edge_box,
        entity    text NOT NULL,
        local_id  bigint NOT NULL,
        hq_id     bigint NOT NULL,
        received_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (edge_id, entity, local_id)
    );
    COMMENT ON TABLE edge_record IS 'HQ: rig row -> HQ row, so re-sent batches are idempotent.';

    -- The API (app_rw) receives batches under the service account's scope.
    GRANT SELECT, INSERT, UPDATE ON edge_box, edge_record TO app_rw;
    """)


def downgrade() -> None:
    run("""
    DROP TRIGGER IF EXISTS edge_outbox ON alert;
    DROP TRIGGER IF EXISTS edge_outbox_update ON alert;
    DROP TRIGGER IF EXISTS edge_outbox ON alert_shelf;
    DROP TRIGGER IF EXISTS edge_outbox ON audio_note;
    DROP TABLE edge_record, edge_box, edge_state, edge_outbox;
    DROP FUNCTION edge_enable(), edge_outbox_push();
    """)
