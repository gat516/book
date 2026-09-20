#!/usr/bin/env bash
# Dedicated test infrastructure only; never reads .env or the developer's live DB.
set -euo pipefail
cd "$(dirname "$0")/../.."
python="${PYTHON_BIN:-services/pipeline/.venv/bin/python}"
export TEST_ADMIN_DATABASE_URL='postgresql://postgres:test-only-password@127.0.0.1:56432/postgres'
export TEST_REDIS_URL='redis://127.0.0.1:56389'
export TEST_OBJECT_ENDPOINT='127.0.0.1:59010'
export TEST_OBJECT_ACCESS_KEY=booktest TEST_OBJECT_SECRET_KEY=test-only-secret
export DATABASE_URL='postgresql://postgres:test-only-password@127.0.0.1:56432/novel_engine'
compose=(docker compose -p book-hosted-checks -f tests/hosted/compose.yml)
trap '"${compose[@]}" down -v' EXIT
"${compose[@]}" up -d --wait --wait-timeout 120
"$python" db/migrate.py --bootstrap
"${compose[@]}" exec -T postgres psql -U postgres -d novel_engine -v ON_ERROR_STOP=1 < db/test-account-isolation.sql
(cd packages/novel-platform && PLATFORM_TEST_DATABASE_URL="$DATABASE_URL" go test ./...)
PRIVATE_TEST_REDIS_URL="$TEST_REDIS_URL" "$python" -m pytest -q services/pipeline/tests/test_private_queue.py
"$python" tests/hosted/http_smoke.py
docker run --rm --network host --user "$(id -u):$(id -g)" --security-opt label=disable \
  -v "$PWD/scripts:/app/scripts:ro" -v "$PWD/db:/app/db:ro" -v "$PWD/tests:/app/tests:ro" \
  -e TEST_ADMIN_DATABASE_URL -e TEST_REDIS_URL \
  -e OBJECT_STORE_ENDPOINT="http://$TEST_OBJECT_ENDPOINT" \
  -e OBJECT_STORE_ACCESS_KEY="$TEST_OBJECT_ACCESS_KEY" -e OBJECT_STORE_SECRET_KEY="$TEST_OBJECT_SECRET_KEY" \
  -e OBJECT_STORE_BUCKET=unused book-python-hosted:test python /app/tests/hosted/recovery.py
