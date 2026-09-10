#!/usr/bin/env bash
# Apply db/migrations in numeric order against DATABASE_URL.
# Simple, forward-only runner for local dev (Milestone 1). Idempotent per file via a
# schema_migrations ledger; production would use a real migration tool.
set -euo pipefail

DATABASE_URL="${DATABASE_URL:-postgres://engine:engine@localhost:5432/novel_engine}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/migrations" && pwd)"

# Parse DATABASE_URL once, for the fallback below. postgres://user:pass@host:port/db
url_rest="${DATABASE_URL#*://}"
url_authority="${url_rest%%/*}"
url_db="${url_rest#*/}"; url_db="${url_db%%\?*}"
url_hostport="${url_authority##*@}"; url_host="${url_hostport%%:*}"
url_userinfo="${url_authority%@*}"
[ "$url_userinfo" = "$url_authority" ] && url_user="engine" || url_user="${url_userinfo%%:*}"

run_psql() {
  if command -v psql >/dev/null 2>&1; then
    psql "$DATABASE_URL" "$@"
  else
    # The compose Postgres already carries psql. Keeping this fallback here lets the
    # one-command local launcher work on a clean host without installing a second client.
    #
    # It must still honour DATABASE_URL. This previously hardcoded `-d novel_engine`,
    # so on a host without psql any other DATABASE_URL was silently ignored and the
    # migrations were applied to the compose database instead -- the one failure mode a
    # migration runner must never have. The container can only reach its own server, so
    # a URL naming another host is refused rather than quietly redirected.
    case "$url_host" in
      localhost|127.0.0.1|::1|"") ;;
      *)
        echo "error: psql is not installed, and the container fallback cannot reach host '$url_host'" >&2
        echo "       install a postgresql client to migrate a non-local database" >&2
        exit 1
        ;;
    esac
    docker exec -i deploy-postgres-1 psql -U "$url_user" -d "$url_db" "$@"
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
