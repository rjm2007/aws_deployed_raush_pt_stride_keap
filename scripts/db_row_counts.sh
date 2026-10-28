#!/usr/bin/env bash
# Exact row count of every table in public + mock_stride, plus each sequence's position.
# Prints "name value" lines only (no row data), so two runs can be compared with diff.
#   scripts/db_row_counts.sh                 # the server's own db (database rpt)
#   scripts/db_row_counts.sh rpt_restore_test
#   SOURCE_DB_URL='postgresql://...' scripts/db_row_counts.sh source   # e.g. Supabase demo
set -euo pipefail

cd "$(dirname "$0")/.."
# COMPOSE_FILE can be overridden (the local rehearsal does); the server uses the default.
export COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
query="
select 'table ' || table_schema || '.' || table_name || ' ' ||
  (xpath('/row/c/text()', query_to_xml(
    format('select count(*) as c from %I.%I', table_schema, table_name), false, true, '')))[1]::text
from information_schema.tables
where table_schema in ('public','mock_stride') and table_type = 'BASE TABLE'
union all
select 'sequence ' || schemaname || '.' || sequencename || ' ' || coalesce(last_value::text, 'unset')
from pg_sequences where schemaname in ('public','mock_stride')
order by 1"

if [[ "${1:-rpt}" == "source" ]]; then
  : "${SOURCE_DB_URL:?set SOURCE_DB_URL}"
  docker compose exec -T -e SOURCE_DB_URL db \
    sh -c 'psql "$SOURCE_DB_URL" -v ON_ERROR_STOP=1 -At -c "$0"' "$query"
else
  docker compose exec -T db psql -U rpt -d "${1:-rpt}" -v ON_ERROR_STOP=1 -At -c "$query"
fi
