#!/usr/bin/env bash
# Apply db/migrations in numeric order against DATABASE_URL.
# Simple, forward-only runner for local dev (Milestone 1). Idempotent per file via a
# schema_migrations ledger; production would use a real migration tool.
set -euo pipefail

DATABASE_URL="${DATABASE_URL:-postgres://engine:engine@localhost:5432/novel_engine}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/migrations" && pwd)"

run_psql() {
  if command -v psql >/dev/null 2>&1; then
    psql "$DATABASE_URL" "$@"
  else
    # The compose Postgres already carries psql. Keeping this fallback here lets the
    # one-command local launcher work on a clean host without installing a second client.
    docker exec -i deploy-postgres-1 psql -U engine -d novel_engine "$@"
  fi
}

run_psql -v ON_ERROR_STOP=1 -q -c \
  "CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now());"

for f in "$DIR"/*.sql; do
  v="$(basename "$f")"
  applied="$(run_psql -tAc "SELECT 1 FROM schema_migrations WHERE version = '$v'")"
  if [ "$applied" = "1" ]; then
    echo "skip  $v"
    continue
  fi
  echo "apply $v"
  run_psql -v ON_ERROR_STOP=1 -q < "$f"
  run_psql -v ON_ERROR_STOP=1 -q -c \
    "INSERT INTO schema_migrations (version) VALUES ('$v');"
done

echo "migrations up to date"
