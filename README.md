# Book

A novel translation and spoiler-aware reader with local development and private hosted accounts.

- [Local service setup](deploy/systemd/README.md)
- [Hosted deployment, migration and recovery](deploy/hosted/README.md)
- [Implementation status](docs/HOSTED_IMPLEMENTATION.md)
- [Build specification](docs/instructions.md) and [agent navigation](AGENTS.md)

## Start the complete project

After configuring `.env`, run:

```bash
make start
```

This starts Docker infrastructure and the pipeline, scraper, APIs, AskAI, and web UI
as supervised user services. With no pending upgrade, healthy processes stay running.
Pending schema upgrades stop application services for migration and restart them afterward;
back up your database and preserve the provider encryption key before upgrading.

Local mode is the default: **localhost does not require Google login**. Keep
`BOOK_MODE=local` in `.env`. The first private-account upgrade preserves the existing
library, consolidates reading progress, and binds encrypted keys to its fixed local owner.

Open <http://localhost:5173/>. To inspect or stop the application services:

```bash
systemctl --user status novel-engine.target
systemctl --user stop novel-engine.target
```

Docker infrastructure can be stopped separately with:

```bash
docker compose -f deploy/docker-compose.yml down
```

## Use hosted AI without Ollama

After the normal app/infrastructure setup, no Ollama installation is required:

1. In **Account settings → Provider keys**, save a Gemini, Groq, DeepSeek,
   Anthropic, or custom API key.
2. When creating a book, choose that provider and a model. In **Book settings**,
   choose separate models for **Translation** (chapter prose) and **AI features**
   (story knowledge and Ask AI). Both use the book’s selected provider.
3. In **Account settings → Semantic search**, choose **Automatic**, **Off**,
   **Gemini**, or **OpenRouter**. Semantic search is optional: without it, Ask AI
   has no semantic chapter search; enable an embedding provider when you need it.

Automatic uses your saved Gemini key (or `GEMINI_API_KEY`) for embeddings; without
one it stays off. Gemini can therefore cover all three roles with one key. Other
completion providers work with search off, or with a separate Gemini/OpenRouter key.
OpenRouter keys in Account settings are for semantic search only.

Keys require `INGEST_PROVIDER_CONFIG_KEY` and `PROVIDER_CONFIG_ENCRYPTION_KEY` to
contain the same base64-encoded 32-byte encryption key. Run `make start` for pending
upgrades. Subsequent settings/key changes are picked up without a restart.

`EMBED_PROVIDER=auto` is the new installation default. **Use server settings**
preserves explicitly configured `EMBED_PROVIDER`/`EMBED_MODEL` values, including
Ollama and the optional gateway. UI choices override those server settings.
Hosted embedding models must support the database’s 768-dimensional vectors.

Search settings affect newly published chapters. Existing chapters are not
re-indexed automatically. Vectors are tagged by provider, endpoint, model, and width;
Ask AI searches only matching vectors. Older untagged vectors are excluded until
re-indexed; chapter text and published story knowledge remain available.

## Private website

Hosted mode uses invited Google accounts, one private library per account, and each
user's own provider keys. It does not share books or fall back to server model keys.
The initial deployment configuration uses `us-east-1`, EC2/k3s, RDS PostgreSQL,
private S3 and persistent Redis. Follow the [hosted runbook](deploy/hosted/README.md)
for the domain, Google OAuth, secrets, deployment and initial-owner invitation steps.
