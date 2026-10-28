#!/usr/bin/env bash
# Nightly backup of the demo server's own Postgres (the `db` container) to S3.
# Runs on the EC2 host from /etc/cron.d/rpt-db-backup (see ../../DB-Move-Runbook.md):
#   BACKUP_S3_BUCKET=<bucket> scripts/db_backup.sh
# pg_dump custom format (-Fc) is already compressed. S3 stores it encrypted (SSE-KMS);
# the upload goes over HTTPS. The bucket's lifecycle rule deletes old copies, not this script.
# AWS access comes from the EC2 instance role, so no keys live on the box.
set -euo pipefail

: "${BACKUP_S3_BUCKET:?set BACKUP_S3_BUCKET}"
BACKUP_S3_PREFIX="${BACKUP_S3_PREFIX:-daily}"
LOCAL_DIR="${BACKUP_LOCAL_DIR:-$HOME/db-backups}"
KEEP_LOCAL="${BACKUP_KEEP_LOCAL:-3}"

cd "$(dirname "$0")/.."
# COMPOSE_FILE can be overridden (the local rehearsal does); the server uses the default.
export COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
compose=(docker compose)

mkdir -p "$LOCAL_DIR"
chmod 700 "$LOCAL_DIR"
umask 077
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
file="$LOCAL_DIR/rpt-demo-$stamp.dump"
# A failed run must not leave a half-written dump behind (also clears any left by older runs).
trap 'rm -f "$file.part"' EXIT
rm -f "$LOCAL_DIR"/rpt-demo-*.dump.part

"${compose[@]}" exec -T db pg_dump -U rpt -d rpt -Fc --no-owner --no-privileges > "$file.part"
# A dump pg_restore cannot list is broken: fail loudly instead of uploading it.
"${compose[@]}" exec -T db pg_restore -l < "$file.part" > /dev/null
mv "$file.part" "$file"

aws s3 cp "$file" "s3://$BACKUP_S3_BUCKET/$BACKUP_S3_PREFIX/$(basename "$file")" \
  --sse aws:kms --only-show-errors

# Keep the newest few on the box for a quick restore; S3 has the rest.
ls -1t "$LOCAL_DIR"/rpt-demo-*.dump | tail -n +"$((KEEP_LOCAL + 1))" | xargs -r rm -f
echo "$(date -u +%FT%TZ) backup ok $(basename "$file") $(stat -c %s "$file") bytes"
