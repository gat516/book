#!/usr/bin/env bash
# Run a command with the repo's .env loaded into the environment.
#
# Nothing in this repo reads .env by itself: pipeline/config.py and ingest-api/config.go
# are both plain getenv-with-fallback, deliberately mirroring each other. Without this
# wrapper every setting silently resolves to its code default, which is how graph
# extraction ended up with a 120s prefill budget while .env said 900.
#
# Shell-level on purpose, so the Go services get exactly the same treatment as the
# Python ones. Real environment variables win over .env, so a one-off override still
# works: GRAPH_OLLAMA_FIRST_TOKEN_SECONDS=60 make worker
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="${BOOK_ENV_FILE:-$root/.env}"

if [ -f "$env_file" ]; then
  # Snapshot the real environment, source .env under `set -a`, then put the snapshot
  # back on top so an explicitly exported variable is never clobbered by the file.
  preset="$(export -p)"
  set -a
  # shellcheck disable=SC1090
  . "$env_file"
  set +a
  eval "$preset"
else
  echo "with-env: no env file at $env_file; using code defaults" >&2
fi

exec "$@"
