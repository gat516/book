# Ask-AI

Internal FastAPI service for spoiler-safe question answering. It accepts only the reader API's bearer-authenticated effective chapter gate, sets the same RLS GUCs inside one transaction, retrieves exact gated context, then calls the shared provider with interactive priority.

Install the local packages during development and run the service:

```bash
pip install -e packages/novel-llm -e services/askai
ASKAI_INTERNAL_TOKEN=local-dev-secret python -m askai
```

`ASKAI_DATABASE_URL` falls back to `READER_DATABASE_URL` and then `DATABASE_URL`.
`LLM_MODEL_ASK` falls back to `LLM_MODEL_EXTRACT`; `EMBED_DIM` must match the configured
embedding model. Ask-AI never accepts a reader identity or raw clearance: the reader API
supplies its already-capped `novel_id`, `question`, and `at` over the internal bearer channel.
