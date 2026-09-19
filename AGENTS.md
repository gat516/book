# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## What this project is

A novel translation + knowledge graph engine: ingests web-serial novels (zh→en),
builds a chapter-versioned knowledge graph (entities, facts, edges), and serves a
spoiler-aware reader experience. **`docs/instructions.md` is the build spec and the single
source of truth** — every design decision derives from its §0 core principles. Read
the relevant section before implementing anything; cite sections (e.g. §5.4) in code
comments and commit messages where the spec drove the decision.

Non-negotiable principles (spec §0, short form):
- **Append-only, chapter-indexed.** Facts are never mutated. Two time columns:
  `source_chapter` (when the reader *learns* it) vs `valid_from_chapter` (when it
  became *true* in-story).
- **Spoiler prevention is authorization, not prompting.** Enforced as a `WHERE` on
  every read path + Postgres RLS, gating on `source_chapter` (knowledge-time), never
  story-time. The LLM must never receive future text.
- **Genre-agnostic:** the ontology is per-novel data (JSON), never hardcoded.
- **Ingestion is offline + idempotent:** content-hash keyed, never on the request path.

## Start here: current paths and focused investigation

The spec is **`docs/instructions.md`**, and the original plan is `docs/PLAN.md`.
Older handoffs may say the root spec is missing or services are stopped; verify current
files and service state instead of treating those historical observations as current.
Use `rg` within the relevant paths below before searching the whole repository.

| Task | Start with |
| --- | --- |
| Hosted provider request/error | `packages/novel-llm/src/novel_llm/hosted.py`, `groq.py`, `provider.py` |
| Provisional term choice / hovercard naming | `services/pipeline/pipeline/display_names.py`, `term_choices.py`, `stages/display_scan.py`; legacy offline name tools: `stages/character_names.py` |
| Retry scheduling / durable failure | `services/pipeline/pipeline/worker.py`, `failures.py`, `batch.py` |
| Reader retry explanation / spoiler gate | `services/reader-api/facts.go`, `store.go`; `services/web/src/factsStatus.ts` |
| Facts controls / chapter status | `services/ingest-api/facts_build.go`; `services/web/src/components/FactsControls.tsx`, `ChapterStatus.tsx`, `ReaderPane.tsx` |
| Translation vs AI provider selection | `services/ingest-api/provider_config.go`; pipeline worker `_provider_for_novel`; `services/askai/askai/app.py` |
| Optional hosted embeddings | `services/ingest-api/embedding_config.go`, `packages/novel-llm/src/novel_llm/embedding_config.py` |
| Running local services | `deploy/systemd/`, `scripts/with-env.sh`; Go units rebuild on restart |

For provider incidents, read **`packages/novel-llm/TROUBLESHOOTING.md`** for the diagnostic
sequence, retry semantics, privacy boundaries, and focused checks. Diagnose the named
chapter first; avoid broad logs, unrelated service exploration, and repeated full suites.

Current additions (September 17, 2026): hosted setup without Ollama and optional semantic
search shipped in `96cd91f` (migration 0108). Groq structured-output recovery and clearer
reader errors use existing DB permissions; **no 0109 migration is required**. The reader
has one top feature-status/control panel. “Build reader features” resumes unfinished
work across the book in chapter order; do not reintroduce a separate chapter build button.

Normal ingestion now skips the standalone `CharacterNamesStage`. Existing display
alignment supplies term types, and `term_choices.py` chooses one stable provisional
spelling using the established Pinyin/foreign-name/title rules, without extra LLM calls.
Hovercards approve/correct it; future translations reuse earlier choices. Saved prose and
approved glossary entries are preserved. See `services/pipeline/README.md`.

## Current state (update as milestones land)

The Milestone-1 vertical slice is complete end-to-end (docs/PLAN.md Phases 1–5): local infra
(compose), migrations through 0008, the Go `ingest-api` paste path, the Python
`pipeline` worker (stages today: CHUNK, TRANSLATE, DISPLAY-SCAN, FACTS,
CHUNK-INDEX), the Go `reader-api` spoiler gate (RLS + app-layer, plus a
chapter-text endpoint), the Rust `textproc` gRPC scanner, the Python `askai` RAG
service, and a one-page React `web` reader UI. Post-Milestone-1 work in progress (see
`.Codex/plans/` for the active phased plan): novel management is done — `GET /novels`,
`GET /novels/{id}` (ungated, reader-api) and `POST /novels` (proxied to ingest-api's now
bearer-token-gated route via `reader-api/ingest.go`), plus a novel picker/create-form in
`web`. The scraper (Phase N5) is also done — new `services/scraper`, two real site
adapters (freewebnovel.com bootstrap, m.shuhaige.net translate — see
`services/scraper/README.md`; an earlier look.twword.com adapter was replaced after its
`robots.txt` turned out to blanket-disallow bots), `scrape_job` (migration 0009) + a
`scrape:pending` Redis queue mirroring the pipeline's own job pattern, reader-api
start/status/cancel endpoints, and a web UI method picker with live polling.
`TranslateStage` gained a bootstrap early-out (`translated_by='external'`) for
chapters that arrive pre-translated. Glossary human-correction (Phase N2) is also done —
`GET /novels/{id}/glossary` (gated on `locked_at_chapter`) + a `PATCH` correction path,
with `ingest-api/glossary.go` porting `glossary_locks.py`'s hash-chain audit invariants to Go
(novel-wide version counter, hand-built JSON to byte-match Python's `json.dumps` exactly
— see `glossary_hash_test.go`'s golden-value cross-language guard), plus a Glossary
toggle in `web`. Forward-only: a correction doesn't retroactively touch already-translated
chapters. Per-novel provider configuration (Phase N3) is also done — a
`novel_provider_config` table (migration 0011, application-level AES-GCM encryption of
API keys via `INGEST_PROVIDER_CONFIG_KEY`, never stored in Postgres in plaintext), an
optional `provider_config` block on `POST /novels`, masked `GET`/`PATCH
/novels/{id}/provider-config` (never decrypts — `api_key_set: bool` only) proxied through
reader-api, plus a `DeepSeekProvider` and `api_key` support added to `AnthropicProvider`
in `packages/novel-llm`. Phase N4 (wiring that config into the request path) is also
done: `pipeline/worker.py` resolves and caches an `LLMProvider`/`BatchManager` per novel
(`_provider_for_novel`, falling back to the process-wide default for a novel with no
`novel_provider_config` row), threaded into `StageContext` via a new `provider_id` field;
`translate.py`'s served-model pin check now compares against that resolved per-novel
provider instead of the process-wide `LLM_PROVIDER` env var directly (a novel pinned to a
different provider than the process default no longer wrongly raises); `askai`'s
`Service` has the identical per-novel cache. Migration 0012 grants askai's connection
role `SELECT` on `novel_provider_config`. Phase N6 (generalized half-translated bootstrap) is also done,
closing out the whole post-Milestone-1 bundle (N1–N6, all six phases): a new
`POST /novels/{id}/glossary/bootstrap` (`ingest-api/glossary.go`'s
`BootstrapGlossaryTerm`, ported from `_lock_glossary`'s "original insert"
path the same way N2's `CorrectGlossaryTerm` was) locks human-supplied source→target
term pairs before any chapter is translated (`locked_at_chapter=0`); the Python
`glossary_locks.py` it was ported from went with RECORDS, and priming (below) is what makes
a locked `target_term` stick. Web's
`AddChapterForm` gained a `BootstrapChapterForm` for pasting a paired raw+translation
chapter with an explicit term-mapping table. **FACTS replaced RECORDS** (`.claude/plans/facts-stage.md`): one model call per
chapter writes tagged `chapter_fact` rows (append-only, 0109/0112), and the spoiler-gated
wiki is assembled from them at read time. Migrations 0087–0093 dropped the legacy graph;
0114 dropped every `record_*`/`fact_first_*` table, `entity`/`alias`,
`novel.active_record_generation`, and the `entity_id` columns on `mention_span`/`glossary`.
FACTS controls: `POST /novels/{id}/facts/{extract,stop}`,
`POST /novels/{id}/chapter/{n}/facts/{retry,discard}`, `GET /novels/{id}/facts/status`.
askai retrieves translated chunks only. The wiki has pages for characters, organizations, places and
items (0115 renamed `character` to `subject` and added `kind`): people come from the names
pass, and the facts prompt (`tagged-facts-v3`) lists the kind of each named organization,
place or item its facts mention; events are a timeline (`GET /novels/{id}/wiki/events`). Not started: Milestone 3 polish beyond this
bundle (retro-update engine, timeline/relationship UI, bulk backfill, multi-novel). Build
order is
spec §11 / PLAN.md for the original slice; the post-slice work follows its own plan
document.

- **Identity is not decided per chapter.** Mention spans and glossary terms are
  terminology only and carry no entity id. Characters (0112) are keyed by source term, and
  aliases (Long Fei = Ling Feng) belong to the wiki-page step, never to a spelling match:
  exact matching *is* the entity-drift bug (§12 risk #2).
- `proto/textproc.proto` is the pinned Python↔Rust contract (§3.3); `services/textproc`
  is the real Rust implementation, selected via `TEXTPROC_BACKEND`. `pipeline/mentions.py`
  keeps the pure-Python fallback behind the same `TextProcClient` interface.
- `mention_span` (migration 0008) holds DISPLAY_SCAN's output — an Aho-Corasick pass
  over the displayed text (the translation, or the source for a same-language book)
  against locked glossary terms and pending name choices. `reader-api`'s `GET /chapter/{n}` is the only
  reader of it; the web UI renders it as highlighted mention spans.

Hosted-only setup now includes account-level semantic-search settings (migration 0108).
`EMBED_PROVIDER=auto` uses an available saved/env Gemini key or leaves search off;
Settings can override it with Gemini, OpenRouter, Off, or the server configuration.
Translation and AI features share the book’s completion provider with separate models;
embeddings use an independent provider. Both workers resolve saved embedding settings
and keys without restarting. Chunk vectors carry `embedding_space` provenance so Ask AI
never compares vectors from different providers/models/endpoints. Older untagged vectors
are excluded from semantic search; existing text and published knowledge are preserved.

## Commands

```bash
# Local infra: Postgres 16 + pgvector, Redis, MinIO
docker compose -f deploy/docker-compose.yml up -d

# Apply migrations (forward-only, ledger-based, idempotent per file)
./db/migrate.sh          # uses DATABASE_URL, defaults to the compose stack

# Run the ingest API (listens :8080; needs INGEST_INTERNAL_TOKEN set)
cd services/ingest-api && INGEST_INTERNAL_TOKEN=<token> go run .

# Run the reader API (listens :8081; needs ASKAI_INTERNAL_TOKEN and INGEST_INTERNAL_TOKEN
# set to the SAME values ingest-api/askai were started with)
cd services/reader-api && ASKAI_INTERNAL_TOKEN=<token> INGEST_INTERNAL_TOKEN=<token> go run .

# Run the scraper worker (drains scrape:pending; needs ingest-api reachable)
cd services/scraper && go run .

# Run the web UI dev server (proxies /api -> reader-api on :8081)
cd services/web && npm install && npm run dev

# Go checks (no test suite yet — add tests alongside new code)
cd services/ingest-api && go vet ./... && go test ./... && go build ./...

# Run the pipeline worker (drains jobs:pending)
cd services/pipeline && .venv/bin/python -m pipeline.worker

# Pipeline tests. Tests marked `db` need the compose Postgres and skip cleanly without
# it, so the pure suite always runs standalone; `-m "not db"` forces that split.
cd services/pipeline && .venv/bin/python -m pytest -q

# Poke the database directly
docker exec -i deploy-postgres-1 psql -U engine -d novel_engine -c "<sql>"
docker exec deploy-redis-1 redis-cli LRANGE jobs:pending 0 -1
```

`services/ingest-api/README.md` has curl examples for the endpoints and how to verify
the three landing zones (Postgres row, MinIO object, Redis queue pointer).

## Architecture notes that span files

- **Monorepo, polyglot by fit** (spec §1): Python = pipeline/LLM/RAG (offline),
  Go = request-path services (ingest-api, scraper, reader-api), Rust = textproc
  (CPU-bound string work over gRPC), TS/React = reader UI.
- **`ChapterEnvelope`** (spec §3.1) is the only thing that crosses the source-adapter
  boundary; the site's chapter number is metadata — the internal `chapter_index` is
  the gate key everywhere.
- **ingest-api is deliberately thin**: hash → object store → `chapter` row
  (`ON CONFLICT DO NOTHING`) → Redis pointer. It does not orchestrate the pipeline.
- **Migrations** are numbered, forward-only SQL in `db/migrations/`; the runner keeps
  a `schema_migrations` ledger. Never edit an applied migration — add a new one.
- **All LLM calls go through the `LLMProvider` protocol** (spec §5.4); pipeline code
  must not import a provider SDK directly. Provider/models come from env
  (`.env.example` mirrors spec §10). Two rules there are load-bearing and easy to miss:
  every call carries a priority `Class`, and `complete()` returns a `Completion` whose
  **`served_model` — not the requested model — is what the cache keys on** (spec §12, §14.3).

## Sibling project: `llm-inference-gateway`

`../llm-inference-gateway` is a rate-limiting / priority-scheduling gateway for LLM
traffic; this repo is its reference client. **It is an optional `LLMProvider` backend, not
a dependency** — everything here must run, demo, and test with it switched off.

- Contract from this side: spec **§14**. From the gateway side: its `docs/spec` **§15**
  (authoritative for gateway behavior).
- Joint build order: gateway spec §15.6, summarized in spec §14.6. Short version — the
  vertical slice never blocks on the gateway; Phase 1.3 just prepares three hooks (§14.2).
- Don't add a hard gateway dependency, and don't let gateway concerns leak into pipeline
  stage code. The protocol is the seam.

## Working with this repo's owner

The owner is building this **to learn** — the project doubles as interview prep.
- **Session start:** read the newest handoff in `.Codex/sessions/` (if any) to pick
  up where the last session stopped.
- **Session end** ("wrap up", "end session", "devlog"): two agents, two audiences —
  the `devlog` agent writes the machine-facing handoff to `.Codex/sessions/`, and
  the `explainer` agent teaches the owner what was built this session and why (pass
  it the list of what changed).
- Explain the "why" behind non-obvious choices as you make them; tie them to spec §0.
  For deeper lessons, use `explainer`; for design tradeoffs, `architect`.
- `notes/` is the owner's own learning space (interview prep, schema reference,
  session template) — human-authored; don't write there unasked, and never fill in
  the `notes/interview-prep.md` blanks for them.
