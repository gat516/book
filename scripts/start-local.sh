#!/usr/bin/env bash
# Bring up the complete local novel-engine stack, including the browser UI.
# Safe to run repeatedly: compose, migrations, unit installation, and service starts are
# all idempotent. instructions.md §5 keeps ingestion offline; this launcher only restores
# the processes that own those already-durable queues.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose_file="$repo/deploy/docker-compose.yml"

if [[ ! -f "$repo/.env" ]]; then
  echo "error: $repo/.env not found" >&2
  echo "       copy .env.example to .env and configure it first" >&2
  exit 1
fi

for command_name in docker systemctl curl npm; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "error: required command not found: $command_name" >&2
    exit 1
  fi
done

echo "Starting Postgres, Redis, MinIO, and textproc..."
docker compose -f "$compose_file" up -d --wait --wait-timeout 120

python="$repo/services/pipeline/.venv/bin/python"
if [[ ! -x "$python" ]]; then
  echo "error: install the pipeline virtualenv first (services/pipeline/README.md)" >&2
  exit 1
fi

# §15: never keep an old process writing across an authorization/schema upgrade.
# The check is read-only, so repeated starts still leave healthy workers running.
pending=0
"$repo/scripts/with-env.sh" "$python" "$repo/db/migrate.py" --check || pending=$?
marker="$repo/.backups/private-upgrade.pending"
if [[ "$pending" == 3 || ( "$pending" == 0 && -f "$marker" ) ]]; then
  echo "Pending schema upgrade: stopping application services for migration..."
  systemctl --user stop novel-engine.target
  mkdir -p "$repo/.backups"
  touch "$marker"
  "$repo/scripts/with-env.sh" "$python" "$repo/db/migrate.py"
  "$repo/scripts/with-env.sh" "$python" "$repo/scripts/migrate_private_library.py"
  rm "$marker"
elif [[ "$pending" != 0 ]]; then
  exit "$pending"
fi

echo "Installing and starting supervised application services..."
"$repo/deploy/systemd/install.sh" --start-only

wait_for_http() {
  local name="$1"
  local url="$2"
  local attempts=30
  local i
  for ((i = 1; i <= attempts; i++)); do
    if curl --max-time 2 -fsS -o /dev/null "$url"; then
      echo "$name ready: $url"
      return 0
    fi
    sleep 1
  done
  echo "error: $name did not become ready: $url" >&2
  return 1
}

wait_for_http "reader API" "http://localhost:8081/novels"
wait_for_http "web UI" "http://localhost:5173/"

echo
echo "Novel engine is ready: http://localhost:5173/"
