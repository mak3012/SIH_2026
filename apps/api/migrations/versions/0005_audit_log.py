"""Append-only, hash-chained audit log.

Revision ID: 0005
Revises: 0004
"""

from alembic import op

revision = "0005"
down_revision = "0004"


def run(sql: str) -> None:
    # Raw driver call: multi-statement SQL, no bind-parameter parsing of ':' or '%'.
    op.get_bind().connection.driver_connection.execute(sql)


def upgrade() -> None:
    run("""
    CREATE TABLE audit_log (
        id           bigint PRIMARY KEY,          -- assigned by the trigger, in chain order
        ts           timestamptz NOT NULL DEFAULT now(),
        actor        text NOT NULL,
        action       text NOT NULL,
        entity       text,
        entity_id    text,
        payload      jsonb NOT NULL DEFAULT '{}',
        prev_hash    bytea NOT NULL,
        payload_hash bytea NOT NULL
    );
    COMMENT ON TABLE audit_log IS
        'Append-only. payload_hash = sha256(prev_hash || audit_canon(row)); '
        'audit_verify() returns the first broken id, NULL if intact.';
    CREATE SEQUENCE audit_log_id_seq OWNED BY audit_log.id;

    -- Canonical text of a row. Fixed formats so the hash does not depend on session settings.
    CREATE FUNCTION audit_canon(r audit_log) RETURNS bytea LANGUAGE sql IMMUTABLE AS $$
        SELECT convert_to(concat_ws(E'\\x1f',
            r.id::text,
            to_char(r.ts AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
            r.actor, r.action, coalesce(r.entity, ''), coalesce(r.entity_id, ''),
            r.payload::text), 'UTF8')
    $$;

    -- SECURITY DEFINER: writers need only INSERT; the chain head is read with owner rights.
    CREATE FUNCTION audit_chain() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = public AS $$
    BEGIN
        -- Serialise writers so ids and prev_hash follow commit order. Held to end of txn.
        PERFORM pg_advisory_xact_lock(hashtext('audit_log'));
        NEW.id := nextval('audit_log_id_seq');
        NEW.prev_hash := coalesce(
            (SELECT payload_hash FROM audit_log ORDER BY id DESC LIMIT 1),
            decode(repeat('00', 32), 'hex'));
        NEW.payload_hash := sha256(NEW.prev_hash || audit_canon(NEW));
        RETURN NEW;
    END $$;
    CREATE TRIGGER audit_chain BEFORE INSERT ON audit_log
        FOR EACH ROW EXECUTE FUNCTION audit_chain();

    CREATE FUNCTION audit_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION 'audit_log is append-only';
    END $$;
    CREATE TRIGGER audit_immutable BEFORE UPDATE OR DELETE ON audit_log
        FOR EACH ROW EXECUTE FUNCTION audit_immutable();
    CREATE TRIGGER audit_no_truncate BEFORE TRUNCATE ON audit_log
        FOR EACH STATEMENT EXECUTE FUNCTION audit_immutable();

    -- Walks the chain. Returns the id of the first row whose links or hash do not match.
    CREATE FUNCTION audit_verify() RETURNS bigint LANGUAGE plpgsql STABLE AS $$
    DECLARE
        r    audit_log;
        prev bytea := decode(repeat('00', 32), 'hex');
    BEGIN
        FOR r IN SELECT * FROM audit_log ORDER BY id LOOP
            IF r.prev_hash <> prev OR r.payload_hash <> sha256(r.prev_hash || audit_canon(r)) THEN
                RETURN r.id;
            END IF;
            prev := r.payload_hash;
        END LOOP;
        RETURN NULL;
    END $$;
    """)


def downgrade() -> None:
    run("""
    DROP FUNCTION audit_verify(), audit_canon(audit_log);  -- depends on the row type
    DROP TABLE audit_log;
    DROP FUNCTION audit_immutable(), audit_chain();
    """)
