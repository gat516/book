#!/usr/bin/env bash
# Apply db/migrations in numeric order against DATABASE_URL.
# Simple, forward-only runner for local dev (Milestone 1). Idempotent per file via a
# schema_migrations ledger; production would use a real migration tool.
set -euo pipefail

DATABASE_URL="${DATABASE_URL:-postgres://engine:engine@localhost:5432/novel_engine}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/migrations" && pwd)"

psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -q -c \
  "CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now());"

for f in "$DIR"/*.sql; do
  v="$(basename "$f")"
  applied="$(psql "$DATABASE_URL" -tAc "SELECT 1 FROM schema_migrations WHERE version = '$v'")"
  if [ "$applied" = "1" ]; then
    echo "skip  $v"
    continue
  fi
  echo "apply $v"
  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -q -f "$f"
  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -q -c \
    "INSERT INTO schema_migrations (version) VALUES ('$v');"
done

echo "migrations up to date"
