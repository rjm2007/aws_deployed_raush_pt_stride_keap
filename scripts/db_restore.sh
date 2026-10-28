#!/usr/bin/env bash
# Restore a pg_dump (-Fc) file into a database in the `db` container.
#   scripts/db_restore.sh <file.dump | s3://bucket/key> <target-db>
# Examples:
#   scripts/db_restore.sh ~/cutover/supabase-demo.dump rpt         # cutover: the new, empty db
#   scripts/db_restore.sh s3://<bucket>/daily/rpt-demo-....dump rpt_restore_test   # restore drill
# Refuses a target that already has tables, so it can never overwrite live data. To replace
# the live db after a disaster: stop every service except db, drop and recreate `rpt`, then run this.
# All in one transaction: any error leaves the target as it was.
set -euo pipefail

src="${1:?usage: db_restore.sh <file.dump | s3://bucket/key> <target-db>}"
target="${2:?usage: db_restore.sh <file.dump | s3://bucket/key> <target-db>}"
[[ "$target" =~ ^[a-z_][a-z0-9_]*$ ]] || { echo "bad database name: $target" >&2; exit 1; }

cd "$(dirname "$0")/.."
# COMPOSE_FILE can be overridden (the local rehearsal does); the server uses the default.
export COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
compose=(docker compose)
psql=("${compose[@]}" exec -T db psql -U rpt -v ON_ERROR_STOP=1 -At)

if [[ "$src" == s3://* ]]; then
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  aws s3 cp "$src" "$tmp/restore.dump" --only-show-errors
  src="$tmp/restore.dump"
fi
[[ -s "$src" ]] || { echo "dump file missing or empty: $src" >&2; exit 1; }

if [[ -z "$("${psql[@]}" -d rpt -c "select 1 from pg_database where datname='$target'")" ]]; then
  "${psql[@]}" -d rpt -c "create database $target" > /dev/null
fi
tables="$("${psql[@]}" -d "$target" -c \
  "select count(*) from pg_tables where schemaname in ('public','mock_stride')")"
if [[ "$tables" != "0" ]]; then
  echo "refusing: $target already has $tables tables" >&2
  exit 1
fi

start=$(date +%s)
# --clean --if-exists replaces the empty `public` schema the new database starts with.
"${compose[@]}" exec -T db pg_restore -U rpt -d "$target" --no-owner --no-privileges \
  --clean --if-exists --single-transaction --exit-on-error < "$src"
# Migration 001 creates pgcrypto; the dump leaves extensions out (Supabase kept them elsewhere).
"${psql[@]}" -d "$target" -c "set client_min_messages = warning; create extension if not exists pgcrypto" > /dev/null
"${psql[@]}" -d "$target" -c "analyze" > /dev/null
echo "restored into $target in $(( $(date +%s) - start )) s"
