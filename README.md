# Book

A local novel translation and spoiler-aware knowledge-graph engine.

## Start the complete project

After configuring `.env`, run:

```bash
make start
```

This starts the Docker infrastructure, applies pending migrations, and starts the
pipeline, scraper, APIs, AskAI, and web UI as supervised user services. It is safe to
run again: healthy application processes are left running, so an active translation is
not interrupted.

Open <http://localhost:5173/>. To inspect or stop the application services:

```bash
systemctl --user status novel-engine.target
systemctl --user stop novel-engine.target
```

Docker infrastructure can be stopped separately with:

```bash
docker compose -f deploy/docker-compose.yml down
```
