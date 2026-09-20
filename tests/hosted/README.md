# Private-hosting checks

Run from repo root on Linux with Docker, Go and the pipeline virtualenv including dev
dependencies. No `.env` is loaded and no model provider is contacted. Google discovery
is fetched during real reader startup; mocked OAuth exchange tests cover identity claims.

```bash
docker build -f deploy/hosted/Dockerfile.go --build-arg SERVICE=reader-api -t book-reader-hosted:test .
docker build -f deploy/hosted/Dockerfile.go --build-arg SERVICE=ingest-api -t book-ingest-hosted:test .
docker build -f deploy/hosted/Dockerfile.python -t book-python-hosted:test .
bash tests/hosted/run.sh
```

The runner creates a dedicated Compose project on ports 56432/56389/59010 and removes
only that project's disposable containers/volumes on exit. The HTTP and recovery tests
create random databases, roles and buckets and clean up their own fixtures. Do not aim
these scripts at a live database or bucket. A killed runner can be cleaned up using
`docker compose -p book-hosted-checks -f tests/hosted/compose.yml down -v`.

Checks include owner RLS plus spoiler gates, account-bound credentials, forged actor
headers, CSRF, private creation and queue controls, immediate deletion authorization,
login-free local mode, read-only backup privileges, object-version erasure, restore
hashes/permissions, session revocation, credential migration/rotation, replay of newer
deletion intent, and restricted-role pipeline/Ask AI startup.

`http_smoke.py` and `recovery.py` can run separately with their documented `TEST_*`
environment variables. Recovery runs inside the Python image to use PostgreSQL 16
clients. For a real AWS rollout, additionally test Google OAuth, TLS, RDS, node IAM,
scheduled backups and a second invitation; local fixtures do not validate those services.

`services/pipeline/.venv/bin/python -m pytest -q tests/hosted/test_transfer_library.py`
runs pure transfer checks (credential rebinding, existing chapter clearance, ownership,
object tampering/missing prose, storage prefixes and migration-ledger compatibility).
The recovery integration also covers versioned pipeline translation objects under
`translated/<novel>/`, not only ingested prose under `novels/<novel>/`.
