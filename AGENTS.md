# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## What this project is

A novel translation + knowledge graph engine: ingests web-serial novels (zh→en),
builds a chapter-versioned knowledge graph (entities, facts, edges), and serves a
spoiler-aware reader experience. **`instructions.md` is the build spec and the single
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

## Current state (update as milestones land)

The Milestone-1 vertical slice is complete end-to-end (PLAN.md Phases 1–5): local infra
(compose), migrations through 0008, the Go `ingest-api` paste path, the Python
`pipeline` worker (stages: CHUNK, TRANSLATE, CHARACTER-NAMES, SCAN, RECORDS,
DISPLAY-SCAN), the Go `reader-api` spoiler gate (RLS + app-layer, plus a
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
term pairs before any entity exists for them (`entity_id NULL`, `locked_at_chapter=0`);
`glossary_locks.py`'s `_lock_glossary` (lifted out of the deleted `resolve.py`, since
terminology locking is not identity) backfills that `NULL` `entity_id` instead of raising
when an entity for the term appears later, and `_decide` overrides the model's proposed
`target_term` with the locked one structurally rather than trusting a prompt hint. Web's
`AddChapterForm` gained a `BootstrapChapterForm` for pasting a paired raw+translation
chapter with an explicit term-mapping table. The **records pipeline** replaced STATE-EXTRACT/GRAPH-WRITE and, with them, the whole
repair/quarantine lifecycle. Knowledge is scoped to an immutable `record_generation`
(prompt, checks, ontology, model, languages) and published per chapter as a `record_run`;
a published run is frozen. Changing any input opens a **new generation** and re-extracts in
chapter order rather than mutating what readers already have. Migrations 0087–0089 added
that storage and dropped the legacy graph; 0090–0093 finished the retreat. Not started: Milestone 3 polish beyond this
bundle (retro-update engine, timeline/relationship UI, bulk backfill, multi-novel). Build
order is
spec §11 / PLAN.md for the original slice; the post-slice work follows its own plan
document.

- **`state.resolutions` is the only way a name becomes an `entity.id`.** The RECORDS
  stage's who's-who pass owns it; nothing else matches names. SCAN retrieves occurrences
  and candidates but never turns a spelling match into an identity decision, and the
  publisher drops a binding whose surface who's-who left unresolved. Exact matching *is*
  the entity-drift bug (§12 risk #2), not an approximation of resolution.
- **Knowledge is typed records in an immutable generation, not `fact`/`edge`/`event`.**
  RECORDS runs discovery (typed XML records citing chapter passages), deterministic code
  checks that drop an individual record without failing the chapter, who's-who identity
  resolution against earlier published chapters, and offline English rendering stored
  apart from the source values. A prompt, ontology, model or source change means a new
  `record_generation` and a chronological rebuild — published extraction is immutable.
- `proto/textproc.proto` is the pinned Python↔Rust contract (§3.3); `services/textproc`
  is the real Rust implementation, selected via `TEXTPROC_BACKEND`. `pipeline/mentions.py`
  keeps the pure-Python fallback behind the same `TextProcClient` interface.
- `mention_span` (migration 0008) holds DISPLAY_SCAN's output — a *second* Aho-Corasick
  pass over the translated text against locked glossary terms, distinct from SCAN's
  extraction-time pass over source text. `reader-api`'s `GET /chapter/{n}` is the only
  reader of it; the web UI renders it as highlighted mention spans.
- `eval/` holds the resolution accuracy metric (workstream A) — run it after any prompt,
  model, or resolve change.

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
