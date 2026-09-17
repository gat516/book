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

## Use hosted AI without Ollama

After the normal app/infrastructure setup, no Ollama installation is required:

1. In **Account settings → Provider keys**, save a Gemini, Groq, DeepSeek,
   Anthropic, or custom API key.
2. When creating a book, choose that provider and a model. In **Book settings**,
   choose separate models for **Translation** (chapter prose) and **AI features**
   (story knowledge and Ask AI). Both use the book’s selected provider.
3. In **Account settings → Semantic search**, choose **Automatic**, **Off**,
   **Gemini**, or **OpenRouter**. Semantic search is optional: without it, Ask AI
   uses published story knowledge rather than searching chapter passages.

Automatic uses your saved Gemini key (or `GEMINI_API_KEY`) for embeddings; without
one it stays off. Gemini can therefore cover all three roles with one key. Other
completion providers work with search off, or with a separate Gemini/OpenRouter key.
OpenRouter keys in Account settings are for semantic search only.

Keys require `INGEST_PROVIDER_CONFIG_KEY` and `PROVIDER_CONFIG_ENCRYPTION_KEY` to
contain the same base64-encoded 32-byte encryption key. Apply migration 0108 and
restart the services when upgrading to this version. Subsequent settings/key changes
are picked up without a restart.

`EMBED_PROVIDER=auto` is the new installation default. **Use server settings**
preserves explicitly configured `EMBED_PROVIDER`/`EMBED_MODEL` values, including
Ollama and the optional gateway. UI choices override those server settings.
Hosted embedding models must support the database’s 768-dimensional vectors.

Search settings affect newly published chapters. Existing chapters are not
re-indexed automatically. Vectors are tagged by provider, endpoint, model, and width;
Ask AI searches only matching vectors. Older untagged vectors are excluded until
re-indexed; chapter text and published story knowledge remain available.
