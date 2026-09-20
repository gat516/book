# Local services

`make start` is the normal entrypoint. Install Docker/Compose, Go, Node/npm, and the
Python environments first; copy `.env.example` to `.env` and configure the internal
tokens and matching provider encryption keys. Example Python installation from repo root:

```bash
python3 -m venv services/pipeline/.venv
services/pipeline/.venv/bin/pip install -e packages/novel-llm -e services/pipeline
python3 -m venv services/askai/.venv
services/askai/.venv/bin/pip install -e packages/novel-llm -e services/askai
npm --prefix services/web ci
make start
```

The units load `.env` through `scripts/with-env.sh`. Go units rebuild on restart. A normal
start leaves healthy workers running; a pending schema upgrade stops application services,
applies SQL and private-library data migration, then starts the new code. A failed upgrade
leaves `.backups/private-upgrade.pending`; fix the cause and rerun `make start`.

Keep `BOOK_MODE=local` for login-free <http://localhost:5173/>. `BOOK_MODE=hosted` requires
the hosted Google/session configuration and must be intentional. If the library fails,
inspect the named API before changing authentication or dropping database policies:

```bash
systemctl --user status novel-reader-api novel-ingest-api novel-web
curl -fsS http://localhost:8081/healthz
curl -fsS http://localhost:8081/novels
journalctl --user -u novel-reader-api -n 30 --no-pager
```

Before upgrading a saved library, take a database backup and retain its encryption key
separately. SQL migration never rewrites chapter objects. Do not use a production/remote
database URL with this local launcher. See [hosted operations](../hosted/README.md) for
portable snapshots and isolated restore rehearsals.

`deploy/systemd/install.sh` installs and restarts units to deploy code; `--start-only`
starts stopped units. Stop applications with `systemctl --user stop novel-engine.target`.
Stopping that target leaves Docker data services running.
