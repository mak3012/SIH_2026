# Runbook: database restore

**When:** the database is lost or corrupted, a migration or bulk change went wrong, or the
weekly restore drill failed. Everything the platform knows is in this one PostgreSQL cluster
([ADR-0001](../../adr/0001-one-postgresql.md)), including Keycloak's database; documents and
recordings are in the S3 object store, volume `s3data` (back that volume up with the host's volume backups).

**Recovery point:** at most 5 minutes of committed data (`archive_timeout`). **Recovery time:**
measured by the drill (`restore_seconds` in `restore-drills.jsonl`).

Before a restore, tell the rigs: alerting from the real-time service stops until the database
is back; rig boxes keep running on their own database and sync when headquarters is back.

## 0. Is a restore needed?

```bash
docker compose ps postgres
```

```bash
docker compose logs --since 30m postgres
```

A container that restarts because of a full disk or a bad setting does not need a restore:
free space or revert the setting. Restore when data files are damaged (`invalid page`,
`could not read block`), the volume is gone, or data must go back to before a mistake.

## 1. What backups are there?

```bash
make backup-info
```

Note the newest full/differential backup and the WAL range (the latest point you can reach).

## 2. Practise first (if time allows)

```bash
make restore-drill
```

This restores into a throwaway container without touching the stack and checks the result
(alembic at head, audit chain intact, wells present). A failure here means fix the backup
first (wrong `BACKUP_CIPHER_PASS`, missing WAL), not restore.

## 3. Restore

Stop everything that writes, keep Postgres's volumes:

```bash
docker compose stop api realtime dagster edge-sync keycloak proxy
```

```bash
docker compose stop postgres
```

Restore into the data volume. Latest point (default), or a point in time before a mistake
(add `--type=time --target="2026-10-08 09:15:00+00" --target-action=promote`):

```bash
docker compose run --rm --no-deps --entrypoint bash postgres -c "pgbackrest --stanza=wells --delta restore"
```

`--delta` reuses files that are still good and replaces the rest. On a new, empty volume leave it
out. Start and watch recovery finish (`database system is ready to accept connections`):

```bash
docker compose up -d postgres
```

```bash
docker compose logs -f postgres
```

## 4. Check

```bash
docker compose exec postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT version_num FROM alembic_version" -c "SELECT audit_verify()" -c "SELECT count(*) FROM well"
```

`audit_verify()` must be empty (NULL). Then verify against the off-box anchors; after a
point-in-time restore, anchors newer than the target are expected to be missing (record that
in the incident):

```bash
make audit-verify
```

## 5. Start the rest

```bash
make up
```

After a point-in-time restore, take a full backup at once (the timeline changed):

```bash
make backup TYPE=full
```

Rig boxes re-send anything headquarters lost: their outbox is only cleared for what was
acknowledged, and HQ accepts re-sent batches idempotently. A rig whose audit chain HQ now sees
as a gap (HQ restored to before the rig's last sync) is refused with 409; reset that box's
watermark at HQ after checking:

```bash
docker compose exec postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "UPDATE edge_box SET last_audit_id = NULL, last_audit_hash = NULL WHERE edge_id = 'rig-07'"
```

## 6. Close

Write down: cause, backup used, target time, `restore_seconds`, data lost (time between the
target and the failure), and any anchors missing. Run `make restore-drill` once more the next
day.
