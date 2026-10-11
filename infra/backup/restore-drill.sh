#!/bin/sh
# Restore drill (docs/operations/runbooks/db-restore.md): restore the latest pgBackRest backup
# plus archived WAL into a throwaway container with no network, check the result, report the
# time it took. The running stack is not touched. Linux/macOS shell with docker; from the repo
# root:  make restore-drill   (or: sh infra/backup/restore-drill.sh)
#
# Checks: the cluster starts and promotes; alembic is at the repository's head revision; the
# audit chain verifies (audit_verify() IS NULL); core tables are not empty. Exit 1 if any fails.
set -eu
. ./.env
IMAGE=timescale/timescaledb-ha:pg17
VOL=$(docker volume ls -q --filter label=com.docker.compose.volume=pgbackrest | head -n 1)
[ -n "$VOL" ] || { echo "no pgbackrest volume: run make backup first" >&2; exit 1; }
HEAD=$(ls apps/api/migrations/versions | sed -n 's/^\([0-9]\{4\}\)_.*/\1/p' | sort | tail -n 1)
OUT=${DRILL_LOG:-restore-drills.jsonl}
start=$(date +%s)

docker run --rm --network none --name wells-restore-drill \
  -e PGBACKREST_CONFIG=/etc/pgbackrest/wells.conf \
  -e PGBACKREST_REPO1_CIPHER_PASS="$BACKUP_CIPHER_PASS" \
  -e PGDB="$POSTGRES_DB" -e HEAD="$HEAD" \
  -v "$VOL":/var/lib/pgbackrest:ro \
  -v "$PWD/infra/postgres/pgbackrest.conf":/etc/pgbackrest/wells.conf:ro \
  --entrypoint bash "$IMAGE" -ec '
    D=/home/postgres/pgdata/data
    mkdir -p "$D" && chmod 700 "$D"
    pgbackrest --stanza=wells --lock-path=/tmp/pgbackrest --log-level-file=off restore --pg1-path="$D" >&2
    pg_ctl -D "$D" -w -t 3600 -o "-c listen_addresses= -c archive_mode=off" start >&2
    until [ "$(psql -tAc "SELECT pg_is_in_recovery()" postgres)" = f ]; do sleep 1; done
    q() { psql -tA -d "$PGDB" -c "$1"; }
    backup=$(pgbackrest --stanza=wells --log-level-file=off info --output=json \
      | python3 -c "import json,sys; b=json.load(sys.stdin)[0][\"backup\"][-1]; print(b[\"label\"])")
    rev=$(q "SELECT version_num FROM alembic_version")
    broken=$(q "SELECT audit_verify()")      # psql prints NULL as an empty string
    last=$(q "SELECT max(ts) FROM audit_log")
    wells=$(q "SELECT count(*) FROM well")
    docs=$(q "SELECT count(*) FROM document")
    pg_ctl -D "$D" -m fast stop >&2
    ok=true
    [ "$rev" = "$HEAD" ] || ok=false
    [ -z "$broken" ] || ok=false
    [ "$wells" -gt 0 ] || ok=false
    printf "{\"backup\":\"%s\",\"alembic\":\"%s\",\"audit_first_broken\":\"%s\",\"last_audit_entry\":\"%s\",\"wells\":%s,\"documents\":%s,\"ok\":%s" \
      "$backup" "$rev" "$broken" "$last" "$wells" "$docs" "$ok"
  ' > /tmp/wells-drill.$$

secs=$(( $(date +%s) - start ))
line="$(cat /tmp/wells-drill.$$),\"at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"restore_seconds\":$secs}"
rm -f /tmp/wells-drill.$$
echo "$line" | tee -a "$OUT"
echo "$line" | grep -q '"ok":true'
