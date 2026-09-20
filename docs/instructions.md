# Novel Translation + Knowledge Graph Engine — Architecture

A system that ingests web-serial novels (Chinese → English), builds a persistent,
chapter-versioned knowledge graph of the world (characters, sects, realms, factions,
events, relationships), and serves a **spoiler-aware** reader experience: hover cards,
an auto-generated wiki, timelines, and a chapter-bounded "Ask AI".

This document is the build spec. It is detailed enough that an agent can scaffold the
repo, write migrations, and implement each service against the contracts defined here.

---

## 0. Core principles (read these first — every decision derives from them)

1. **The knowledge layer is the product, not the translation.** Translation is a
   commodity; persistent world-state + entity resolution + spoiler-aware retrieval is
   the differentiator. Optimize for those.

2. **Everything is chapter-indexed and append-only.** No fact is ever mutated. Rows carry
   two chapter columns with different jobs: `source_chapter` (the chapter the reader
   *learns* it — knowledge-time) and `valid_from_chapter` (the chapter it became *true* —
   story-time). A reader at chapter `N` may see only rows with `source_chapter <= N`.

3. **Spoiler prevention is an authorization problem, enforced at retrieval — never in a
   prompt.** A reader is a principal "cleared" up to chapter `N`. The gate is a `WHERE`
   clause on every read path and a pre-filter on RAG retrieval, and it gates on
   **knowledge-time (`source_chapter`), never story-time** — a chapter-500 flashback
   revelation about chapter 10 must stay hidden at chapter 220. The LLM must never
   physically receive future text. "Don't spoil" in a system prompt is a non-solution.

4. **Genre-agnostic by construction — the ontology is data, not code.** The graph
   (`entity` + `fact` + `edge`) represents *anything*: sects and realms for xianxia,
   houses and bloodlines for epic fantasy, suspects and clues for mystery, stats and
   skills for litRPG. The only genre-specific things are a per-novel **ontology**
   (which entity kinds / attributes / relation types to look for) and the extraction
   prompt that reads it. Both are configuration, chosen by genre preset or auto-induced
   (§5.1). Nothing about cultivation is hardcoded.

5. **Translation is an optional stage, not a given.** It runs only when
   `source_lang != target_lang`. For a same-language novel the translate step is skipped
   entirely; entity-name consistency still matters and is handled by resolution (§5), but
   there is no glossary term-locking to do. The glossary/lock machinery exists for
   translated works.

6. **Extract before you translate (when translating).** Resolve entity identity in the
   source language (unambiguous there), lock canonical translations in a glossary, then
   translate with the glossary as a hard constraint — what keeps "青云宗" from becoming
   three different English names across 4,000 chapters.

7. **Ingestion is offline and idempotent.** Background job system, never on the request
   path. Every step is content-hash keyed so re-runs are free and crashes recoverable.
   Cost is the real constraint → cache aggressively, batch everything batchable.

8. **Source is an adapter detail.** Paste and scrape both produce one normalized
   envelope. Nothing downstream knows or cares where a chapter came from.

---

## 1. Tech stack & language split

Polyglot by deliberate fit, not preference. Each language owns what it is best at.

| Layer | Language | Why |
|---|---|---|
| Pipeline orchestration, LLM calls, extraction, glossary resolution, translation, state extraction, RAG | **Python** | Best LLM/SDK/NLP/embeddings ecosystem; ingestion is offline so latency is irrelevant |
| Reader-facing API, auth, chapter-gate query service, scraper fetch loop | **Go** | Concurrency, robust HTTP, clean static binaries; the identity-aware/security surface |
| Mention scanner (alias matching), retro-update span engine, content dedup hashing | **Rust** | CPU-bound, high-throughput string work over large corpora; Aho-Corasick over thousands of aliases |
| Reader UI (hover cards, wiki, maps, timeline, Ask-AI) | **TypeScript / React** | Standard |

**Shared infrastructure**

- **Postgres 16 + pgvector** — single source of truth: entities, aliases, facts, edges,
  glossary, chapters, embeddings. One store keeps the MVP simple; queries here are
  filtered lookups and shallow joins, not deep traversals, so a dedicated graph DB is
  not needed until relationship-diagram queries prove slow.
- **Redis** — job queue + caches (see §6).
- **Object storage** — S3 (prod) / MinIO (local): raw HTML, raw text, translated text.
- **gRPC** — Python ↔ Rust contract (the Rust `textproc` service).
**LLM provider — swappable, selected by env var**

All LLM calls go through a `LLMProvider` protocol (§5.4); no pipeline code imports a
provider SDK directly. Three backends ship:

| Backend | Best for | Notes |
|---|---|---|
| **Anthropic** (Haiku 4.5 / Sonnet 4.6 / Opus 4.8) | Production, translation quality | Batch API → 50% offline discount (input **and** output). Prompt caching is **not automatic** — it requires explicit `cache_control` markers (§6.2); front-loading alone caches nothing here |
| **DeepSeek** (V4 family: `deepseek-v4-flash`, `deepseek-v4-pro`) | Cost-sensitive runs, zh→en | ~10× cheaper (V4 Flash ≈ $0.14 / $0.28 per M in/out cache-**miss**); no *synchronous* Batch API, but **automatic** disk prefix caching drops cached input to ≈$0.0028/M (~**98%** off, not 90%); off-peak scheduling (16:30–00:30 UTC) historically 50–75% off but **unconfirmed for V4** — treat as opportunistic, not budgeted; data routes to CN servers. Pin explicit V4 IDs — the `deepseek-chat`/`deepseek-reasoner` aliases **retire 2026-07-24 15:59 UTC** and then error |
| **Ollama / local** (Qwen3, Mistral) | Dev, private content | Free; data stays on device; `qwen3` (e.g. `qwen3:8b`) is the current best local choice for CJK text (supersedes qwen2.5); no batching |

Use a stronger model for translation (`LLM_MODEL_TRANSLATE`) and a cheaper one for
extraction (`LLM_MODEL_EXTRACT`). The embed model is separate and can be local
(nomic-embed-text via Ollama) regardless of which completion backend is active.

A fourth backend, **`gateway`**, is optional and arrives late (§14): it routes all of the
above through the sibling `llm-inference-gateway` project for cross-process rate limiting
and interactive-vs-batch priority. It is a `LLMProvider` implementation like any other and
the system must run fully without it. Read §14 before writing any code that assumes a
requested model is the model that answered.

---

## 2. Repository layout (monorepo)

```
novel-engine/
├── proto/                     # shared gRPC/protobuf contracts (textproc.proto, envelope.proto)
├── db/
│   └── migrations/            # SQL migrations (numbered, forward-only)
├── services/
│   ├── ingest-api/    (Go)    # paste endpoint, enqueue, novel registration
│   ├── scraper/       (Go)    # fetch loop, politeness, resume, extraction
│   ├── reader-api/    (Go)    # auth, chapter gate, read endpoints
│   ├── pipeline/      (Py)    # extraction · glossary · translate · state · batch manager
│   ├── askai/         (Py)    # chapter-filtered RAG + sync LLM
│   └── textproc/      (Rust)  # gRPC: mention scan, retro-update, dedup hash
├── web/               (TS)    # React reader UI
└── deploy/
    ├── docker-compose.yml     # local: postgres, redis, minio, all services
    └── k8s/                   # optional prod manifests
```

---

## 3. Cross-service contracts (define these before any implementation)

### 3.1 ChapterEnvelope — the normalized ingestion unit

Produced by every source adapter, consumed by the pipeline. Nothing else crosses the
adapter boundary.

```jsonc
{
  "novel_id":      "uuid",
  "chapter_index": 453,            // INTERNAL canonical index, not the site's number
  "raw_text":      "……",          // extracted source-language body, no nav/ads
  "source_lang":   "zh",
  "source_meta": {
    "source_url":     "https://site/novel/chapter-453",
    "site_chapter_no": "453",      // what the site printed; metadata only
    "fetched_at":     "2026-06-30T00:00:00Z",
    "raw_hash":       "sha256:...",// hash of raw_text; drives dedup + cache keys
    "adapter":        "scrape|paste"
  }
}
```

> The site's chapter number is **metadata**, never the gate key. Aggregators have
> prologues, "chapter 0", and side chapters that break 1:1 numbering. The reader's
> progress is always the internal `chapter_index`.

### 3.2 SourceAdapter interface (Go)

```go
type SourceAdapter interface {
    // Fetch a specific chapter (used by paste and by increment-mode scraping).
    Fetch(ctx context.Context, novelID string, idx int) (ChapterEnvelope, error)
    // Follow "next chapter" from a known page (robust scraping; see §7).
    Next(ctx context.Context, prev ChapterEnvelope) (ChapterEnvelope, bool, error)
}
```

### 3.3 textproc gRPC (Rust) — pin the `.proto`, both sides generate from it

This is the Python↔Rust contract; write it as a literal artifact **now** (the Phase-1
Python stub is shaped from the same file) so the Phase-4 Rust swap is a transport change,
not a refactor. Messages return **both byte and char offsets** — `aho-corasick` matches on
bytes and returns byte offsets, but the display layer indexes UTF-8 by character, so the
service maps them once at the source rather than making every caller redo it.

```proto
syntax = "proto3";
package textproc;

service TextProc {
  // Aho-Corasick over an alias set → mention spans, for retrieve-then-resolve.
  rpc ScanMentions (MentionScanRequest) returns (MentionScanResponse);
  // Deterministic canonical-term replacement across cached translations (retro-update).
  rpc ApplyRetroUpdate (RetroRequest) returns (RetroResponse);
  // Content hashing / near-dup detection for cache keys + scrape stop-conditions.
  rpc HashContent (HashRequest) returns (HashResponse);
}

message Alias { string alias_id = 1; string surface = 2; }

message MentionScanRequest {
  string text     = 1;
  repeated Alias aliases = 2;
  string lang     = 3;      // BCP-47; selects the post-match boundary filter (§5.3)
}
message Span {
  string alias_id  = 1;
  uint32 byte_start = 2; uint32 byte_end = 3;   // aho-corasick native (bytes)
  uint32 char_start = 4; uint32 char_end = 5;   // mapped for the display layer
}
message MentionScanResponse { repeated Span spans = 1; }

message HashRequest  { string text = 1; }
message HashResponse { string sha256 = 1; string near_dup_sig = 2; }

message RetroRequest  { string text = 1; repeated Replacement replacements = 2; }
message Replacement   { string from = 1; string to = 2; }
message RetroResponse { string text = 1; }
```

> **Aho-Corasick semantics (§5.3 relies on this):** the crate matches exact byte
> substrings *without* segmentation — 王 is found inside 王国 regardless. Segmentation
> (jieba/lindera) is **not** on the match path; it is a *post-match boundary filter* that
> suppresses false positives (the surname 王 matched inside the unrelated word 王国
> "kingdom"). Use `MatchKind::LeftmostLongest` to prefer 王国 over 王 where appropriate.
> Toolchain is pinned in §3.6 — do not let an agent wire a mandatory segmenter into the
> matcher.

### 3.4 Redis queue message — the ingest-api↔pipeline contract

The list holds a **small JSON pointer, not the full `ChapterEnvelope`**. Authoritative job
state (raw_hash, config versions, status) lives in the `job`/`chapter` rows and is hydrated
from Postgres on dequeue. Conflating the envelope and the queue message is how two agents in
two sessions invent incompatible shapes — so the bytes on the list are fixed here:

```jsonc
{
  "job_id":          "uuid",
  "novel_id":        "uuid",
  "chapter_index":   453,
  "stage":           "chunk|resolve|translate|state|graph_write",
  "idempotency_key": "sha256:...",     // §3.5
  "enqueued_at":     "2026-06-30T00:00:00Z"
}
```

Producer (`ingest-api`, Go) `LPUSH`es this; the worker (§5) `BLMOVE jobs:pending
jobs:processing RIGHT LEFT <timeout>` to claim it (see §6.3 for the reliable-queue +
reaper details). `ChapterEnvelope` (§3.1) is what the pipeline *reconstructs* from Postgres
after dequeue — it never travels on the queue.

A reader's explicit priority request may atomically move only the requested chapter
pointer to the RIGHT of `jobs:pending`, after checking `jobs:processing`. It must
not flush other work or interrupt an active chapter. This is scheduling only:
chapter completion and graph authorization gates are unchanged.


### 3.5 Idempotency key — one canonical function, used everywhere

The key must be **byte-identical** across Go and Python or the cache silently misses (waste)
or, worse, collides (stale output served). Define it once as a shared helper; the two docs
previously disagreed (`config_version` vs `glossary_version`/`ontology_version`), so this is
now the single source of truth:

```
key = "sha256:" + sha256(
        stage + "\x1f" +               // ASCII unit separator 0x1F between fields
        raw_hash + "\x1f" +
        prompt_version + "\x1f" +
        stage_config_version + "\x1f" + // resolves per-stage (see below)
        model_id                        // = provider + ":" + model, e.g. "deepseek:v4-flash"
      )
```

- `stage_config_version` resolves to `glossary_version` for **translate**,
  `ontology_version` for **state**-extract, etc. — a per-stage input, not a global one.
- Fields are UTF-8, joined by the literal byte `0x1F`; ints are decimal strings.
- `model_id` is **non-optional** — omit it and a backend switch serves the previous
  provider's cached output under a still-valid key (silent corruption, §12).
- The **resolve** stage is *not* content-cached (its output depends on live DB state) — see
  the reconciliation note under the `job` table in §4 and the Phase-1 "Done when" in the plan.

### 3.6 Rust gRPC toolchain (pin the versions)

Tonic 0.14 split the prost codec out of the core crate; an agent trained on tonic ≤0.12 will
write a `build.rs` against the old all-in-one path that no longer exists. Pin:

- `tonic` **0.14.x**, with codegen via **`tonic-prost-build`** (not the old `tonic-build`
  prost path). `tonic-prost-build` v0.14.x requires **Rust ≥ 1.88.0**.
- `build.rs` uses `tonic_prost_build::configure().compile_protos(&["proto/textproc.proto"], &["proto"])`.
- Python side generates with `grpcio-tools` / `protobuf` from the same `.proto`.

(Tonic now lives under the CNCF gRPC project as `grpc/grpc-rust` and is in bugfix/feature-
freeze mode, so 0.14.x is a stable target.)

---

## 4. Data model (Postgres DDL — authoritative)

```sql
-- ---------- Novels & chapters ----------
CREATE TABLE novel (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  title        TEXT NOT NULL,
  source_lang  TEXT NOT NULL DEFAULT 'en',
  target_lang  TEXT NOT NULL DEFAULT 'en',  -- translate stage runs only if source != target
  genre        TEXT,                         -- selects an ontology preset; NULL → auto-induce
  ontology     JSONB NOT NULL,               -- ACTIVE ontology for this novel (see §4.1)
  url_template TEXT,                          -- e.g. https://site/n/chapter-{n}; NULL if paste-only
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE chapter (
  novel_id       UUID NOT NULL REFERENCES novel(id),
  chapter_index  INT  NOT NULL,           -- internal canonical index (the gate key)
  raw_hash       TEXT NOT NULL,           -- sha256 of source body
  raw_uri        TEXT NOT NULL,           -- object-store key, source body
  translated_uri TEXT,                    -- object-store key, target body (NULL if no translation)
  translated_by  TEXT,                    -- provenance: provider+model that produced the translation
                                          -- (e.g. deepseek/v4-flash). Lets you find + selectively
                                          -- re-translate cheap-model chapters later; NULL if untranslated.
  glossary_version INT,                   -- glossary version used; NULL when source==target
  source_meta    JSONB NOT NULL,
  status         TEXT NOT NULL DEFAULT 'ingested', -- ingested|extracted|translated|done|error
  PRIMARY KEY (novel_id, chapter_index)
);

-- ---------- Entities, aliases, glossary ----------
CREATE TABLE entity (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  novel_id    UUID NOT NULL REFERENCES novel(id),
  kind        TEXT NOT NULL,              -- a kind defined in novel.ontology (not a fixed enum)
  canonical   TEXT NOT NULL,             -- canonical name (target lang)
  first_seen_chapter INT NOT NULL,
  embedding   VECTOR(768)                -- pgvector: for retrieve-then-resolve.
                                          -- 768 = nomic-embed-text output; MUST equal
                                          -- EMBED_DIM and the chunk column (see §1.1 fix).
);
CREATE INDEX ON entity USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 200);
-- ANN is acceptable HERE (unlike chunk): resolution retrieval is a fuzzy candidate
-- search, not a security boundary, and a missed candidate is caught by exact alias
-- match in the same step. Use HNSW, not ivfflat: HNSW builds on an empty table (no
-- training-data requirement) and handles the continuous inserts ingestion produces,
-- whereas ivfflat is useless-to-harmful on the tiny/early entity table (lists≈rows/1000
-- ⇒ ~1 meaningless cluster) and drifts under inserts, needing scheduled REINDEX. HNSW
-- drops the whole "build only after seed data exists" dance.

CREATE TABLE alias (
  entity_id   UUID NOT NULL REFERENCES entity(id),
  surface     TEXT NOT NULL,             -- a name/epithet in source OR target lang
  lang        TEXT NOT NULL,             -- BCP-47 code (zh, ja, en, ...)
  first_seen_chapter INT NOT NULL,       -- SPOILER-CRITICAL: gate aliases like facts.
                                          -- An epithet earned at ch.900 ("Heavenly Saint")
                                          -- or an identity reveal (masked man = mentor)
                                          -- must not appear on a ch.50 hover card.
  PRIMARY KEY (entity_id, surface, lang)
);
CREATE INDEX ON alias (surface);

CREATE TABLE glossary (
  novel_id    UUID NOT NULL REFERENCES novel(id),
  source_term TEXT NOT NULL,             -- e.g. 青云宗
  target_term TEXT NOT NULL,             -- e.g. Azure Cloud Sect (LOCKED)
  entity_id   UUID REFERENCES entity(id),
  version     INT  NOT NULL DEFAULT 1,
  locked_at_chapter INT NOT NULL,
  deleted BOOLEAN NOT NULL DEFAULT false, -- human deletion tombstone (0019)
  PRIMARY KEY (novel_id, source_term)
);

-- Audit trail for retroactive glossary changes (tamper-evident, append-only).
CREATE TABLE glossary_changelog (
  id          BIGSERIAL PRIMARY KEY,
  novel_id    UUID NOT NULL,
  seq         INT  NOT NULL,              -- per-novel monotonic sequence (see below)
  source_term TEXT NOT NULL,
  old_target  TEXT,
  new_target  TEXT NOT NULL,
  changed_at_chapter INT NOT NULL,
  prev_hash   TEXT,                       -- hash-chain over (prev_hash || row) for tamper evidence
  row_hash    TEXT NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (novel_id, seq)                  -- a forked chain fails LOUDLY instead of silently
);
-- Concurrency model (a per-row hash chain is only well-defined under serialized appends):
-- take pg_advisory_xact_lock(hashtext(novel_id::text)) around each append so two writers
-- can't read the same prev_hash and fork the chain. Glossary writes are low-volume, so the
-- lock is free; the UNIQUE(novel_id, seq) is the belt-and-suspenders that turns any missed
-- lock into an error rather than corruption.

-- ---------- Facts, edges (append-only, chapter-versioned) ----------
-- TWO time columns with DIFFERENT jobs:
--   source_chapter     = when the READER LEARNS it  → the AUTHORIZATION key (gate/RLS)
--   valid_from_chapter = story-time it became true  → display/ordering only
-- Gating on valid_from alone leaks flashback revelations (a chapter-500 reveal about
-- chapter 10 has valid_from=10 and would show at chapter 220). Gate on source_chapter.
CREATE TABLE fact (
  id            BIGSERIAL PRIMARY KEY,
  novel_id      UUID NOT NULL REFERENCES novel(id),   -- needed for novel-scoped RLS
  entity_id     UUID NOT NULL REFERENCES entity(id),
  attribute     TEXT NOT NULL,           -- realm|status|title|location|...
  value         TEXT NOT NULL,
  kind          TEXT NOT NULL DEFAULT 'assertion',  -- assertion|retraction|correction
  supersedes    BIGINT REFERENCES fact(id),         -- points at the row this corrects/retracts
  valid_from_chapter INT NOT NULL,        -- story-time: when it became true
  source_chapter INT NOT NULL,            -- knowledge-time: chapter it was extracted from
  confidence    REAL NOT NULL DEFAULT 1.0
);
CREATE INDEX ON fact (entity_id, attribute, source_chapter, valid_from_chapter);
-- CORRECTION without mutation (still append-only, §0.2): LLM extraction WILL produce wrong
-- facts. A strict "only a later valid_from supersedes" model can't fix an error as-of the
-- SAME story-time. So a fact row carries a `kind` and an optional `supersedes` FK: a
-- 'retraction'/'correction' row (with its own source_chapter, so it too is gated) points at
-- the bad row; as-of resolution skips any fact that a visible retraction/correction targets.
-- DETERMINISTIC TIEBREAK for the as-of query below (LIMIT 1 must not pick arbitrarily among
-- facts sharing valid_from): order by valid_from_chapter DESC, source_chapter DESC,
-- confidence DESC, id DESC.

CREATE TABLE edge (
  id            BIGSERIAL PRIMARY KEY,
  novel_id      UUID NOT NULL REFERENCES novel(id),   -- needed for novel-scoped RLS
  src_id        UUID NOT NULL REFERENCES entity(id),
  dst_id        UUID NOT NULL REFERENCES entity(id),
  rel_type      TEXT NOT NULL,           -- teacher|enemy|ally|member_of|...
  valid_from_chapter INT NOT NULL,
  valid_to_chapter   INT,                -- null = still in effect
  source_chapter INT NOT NULL            -- knowledge-time: the authorization key
);
CREATE INDEX ON edge (src_id, source_chapter);

CREATE TABLE event (                      -- timeline rows
  id            BIGSERIAL PRIMARY KEY,
  novel_id      UUID NOT NULL REFERENCES novel(id),
  chapter_index INT NOT NULL,
  summary       TEXT NOT NULL,
  entity_ids    UUID[] NOT NULL
);
CREATE INDEX ON event (novel_id, chapter_index);
-- SPOILER LEAK THROUGH THE ARRAY: a UUID[] carries no FK and RLS on the event row gates
-- whether you see the EVENT, not whether each referenced entity is visible at chapter N. An
-- event visible at N can reference an entity with first_seen_chapter > N. When rendering an
-- event, RE-FILTER entity_ids through the RLS-gated `entity`/`alias` visibility (join; never
-- trust the raw array). Alternatively store (event_id, entity_id) junction rows so RLS gates
-- the junction directly. This case belongs in the flashback regression suite (§12).

-- ---------- RAG chunks (chapter-filtered retrieval) ----------
CREATE TABLE chunk (
  id            BIGSERIAL PRIMARY KEY,
  novel_id      UUID NOT NULL REFERENCES novel(id),
  chapter_index INT NOT NULL,            -- the RAG gate key
  text          TEXT NOT NULL,           -- target-lang chunk (source text if untranslated)
  embedding     VECTOR(768) NOT NULL     -- 768 = nomic-embed-text; MUST equal EMBED_DIM
                                          -- and the entity column. Changing models requires
                                          -- a re-embed migration (new col + backfill +
                                          -- reindex — pgvector has no in-place dim change).
);
CREATE INDEX ON chunk (novel_id, chapter_index);
-- EMBEDDING-LANGUAGE INVARIANT: chunks must be embedded in the SAME language the question
-- will be asked in. Translated novels store English `text` → nomic-embed-text is fine
-- (English is its strength). If you ever embed SOURCE-language (zh) chunks, switch to a
-- multilingual model (bge-m3 / nomic v1.5 / qwen3-embedding) — but keep the dimension equal
-- to EMBED_DIM (§1.1), which forces a matching-dim model, not a 1024-dim one on a 768 col.
--
-- Deliberately NO ANN index at this scale. A 4,000-chapter novel is ~40K chunks;
-- an exact scan scoped by (novel_id, chapter_index <= N) is fast AND exactly correct.
-- ANN (ivfflat/HNSW) post-filters, so a chapter-gate WHERE clause can silently drop
-- results near the boundary — unacceptable when the filter is a security boundary.
-- pgvector 0.8 added iterative index scans (hnsw.max_scan_tuples / ivfflat.max_probes) that
-- soften the general "ANN can't filter" argument — but they still bound themselves by a
-- threshold and can UNDER-return at a hard boundary, so exact scan remains correct FOR THE
-- SPOILER BOUNDARY specifically. Revisit HNSW + 0.8 iterative scan only for NON-security
-- queries, or if per-novel corpora grow far beyond this.

-- ---------- Jobs & batches ----------
CREATE TABLE job (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  novel_id      UUID NOT NULL,
  chapter_index INT NOT NULL,
  stage         TEXT NOT NULL,           -- extract|resolve|translate|state
  state         TEXT NOT NULL DEFAULT 'pending', -- pending|batched|running|done|error|deadletter
  idempotency_key TEXT NOT NULL,         -- the canonical §3.5 function, computed by ONE shared
                                          -- helper in every service (Go + Python) so it is
                                          -- byte-identical: sha256(stage ‖ raw_hash ‖
                                          -- prompt_version ‖ stage_config_version ‖ model_id),
                                          -- 0x1F-separated. stage_config_version = glossary_version
                                          -- for translate, ontology_version for state. model_id =
                                          -- provider:model (e.g. deepseek:v4-flash) — omitting it
                                          -- serves another provider's cached output after a backend
                                          -- switch (silent corruption).
  batch_id      TEXT,                    -- provider batch id when submitted
  attempts      INT NOT NULL DEFAULT 0,
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (idempotency_key)               -- dedup: never run the same input twice
);
-- NOTE: the RESOLVE stage reads the live alias index, so its output is a function of
-- database state, not just content. Two options, pick (b) so "zero LLM calls on identical
-- re-paste" is literally true (§11 Milestone-1 done-when, §3.5):
--   (a) leave resolve UNCACHED — then re-pasting an identical chapter DOES re-run resolve's
--       LLM calls, so the acceptance test must read "zero LLM calls for CACHED stages
--       (translate, state); resolve may re-run if the alias index changed."
--   (b) cache resolve CONDITIONALLY by folding an alias-index version/hash into
--       stage_config_version — an unchanged index yields a cache hit, so an identical
--       re-paste with an unchanged index is genuinely zero-call. Preferred.
-- Either way, translate/state are pure functions of (content, prompt, config-version).
```

### 4.1 The ontology (what makes it genre-agnostic)

`novel.ontology` is the single piece of configuration that adapts the whole system to a
genre. It declares the entity kinds, the attributes worth tracking, and the relation
types — and the extraction prompt (§5) is templated on it. Same code, different JSON per
novel.

```jsonc
{
  "kinds":      ["character", "faction", "location", "item", "power"],
  "attributes": [                       // tracked per entity, time-versioned as facts
    { "name": "status",   "kinds": ["character"] },   // alive/dead/missing/...
    { "name": "rank",     "kinds": ["character"] },   // realm / level / title / station
    { "name": "affiliation", "kinds": ["character"] },
    { "name": "leader",   "kinds": ["faction"] },
    { "name": "state",    "kinds": ["faction"] }      // active/destroyed/at-war/...
  ],
  "relations":  ["member_of", "ally", "enemy", "mentor", "family", "located_in"]
}
```

**Genre presets** (seed data, picked by `novel.genre`) — one JSON each, no code change:

| Genre | Example kinds | Example tracked attributes |
|---|---|---|
| Xianxia / cultivation | character, sect, realm, technique, artifact, beast | rank (realm), status, affiliation |
| Epic fantasy | character, house, kingdom, location, magic_system | title, allegiance, status |
| Mystery / crime | character, suspect, clue, location, organization | suspicion, alibi, status |
| LitRPG / progression | character, skill, item, quest, guild | level, class, hp/stats |
| Romance / drama | character, family, location, organization | relationship_status, occupation |
| Generic (fallback) | character, group, location, item | status, role |

The presets are conveniences. The graph never validates against them at write time —
`kind` and `attribute` are free text — so an extractor that discovers a kind the preset
missed just writes it. The preset only shapes *what the prompt asks for*.

### 4.2 Auto-induction (no preset, unknown novel)

If `genre` is NULL, a one-time **schema-induction** pass runs over the first K chapters
(e.g. 5–10): the LLM is asked to propose the entity kinds, key tracked attributes, and
relation types this novel actually uses. The result is written to `novel.ontology` and
locked. This is genuinely useful engineering (schema induction over a corpus) and means
the system handles a novel it has never seen without anyone hand-authoring an ontology.
Re-induction can be triggered manually if early chapters were unrepresentative.

```sql
-- Latest value of ANY tracked attribute for a reader at chapter N.
-- TWO conditions, different jobs:
--   source_chapter <= N      → authorization: the reader has READ where this was stated
--   valid_from_chapter <= N  → correctness: it is in effect (excludes known prophecies)
-- 'realm' is one example — the attribute name comes from the ontology, not code.
SELECT value FROM fact
WHERE entity_id = $1 AND attribute = $attr
  AND source_chapter <= $N AND valid_from_chapter <= $N
ORDER BY valid_from_chapter DESC, source_chapter DESC LIMIT 1;

-- Active relationships at chapter N (same two-condition pattern):
SELECT * FROM edge
WHERE src_id = $1 AND source_chapter <= $N AND valid_from_chapter <= $N
  AND (valid_to_chapter IS NULL OR valid_to_chapter > $N);

-- Aliases safe to display at chapter N (identity reveals stay hidden):
SELECT surface FROM alias WHERE entity_id = $1 AND first_seen_chapter <= $N;

-- Timeline up to N:
SELECT chapter_index, summary FROM event
WHERE novel_id = $1 AND chapter_index <= $N ORDER BY chapter_index;
```

---

## 5. Ingestion pipeline (Python `pipeline` service)

Runs offline against the job queue. The novel's ontology (§4.1) is resolved once up front
(preset by genre, or auto-induced — §4.2) and passed to every extraction prompt. Per
chapter, in order:

```
ChapterEnvelope  (+ novel.ontology, resolved once per novel)
   │
   ▼
1. CHUNK            split body on sentence/paragraph boundaries, respect context window
   ▼
2. TRANSLATE        READER-CRITICAL PATH. ONLY IF source_lang != target_lang. LLM,
                    the previously locked glossary snapshot injected as HARD constraints.
                    Save validated prose and set translation_ready immediately. [Py + LLM]
   ▼
3. CHARACTER NAMES  inventory exact source surfaces and record review candidates.
                    Deterministic locks affect later chapters, never rewrite step 2. [Py + LLM]
   ▼
4. MENTION SCAN     textproc.ScanMentions(text, alias_set)  → candidate spans  [Rust]
   ▼
5. RESOLVE          retrieve-then-resolve: for each mention, pull candidate entities
                    (vector sim + exact alias), LLM CONFIRMS/DISAMBIGUATES against
                    candidates only — never free-generates. Entity kinds come from
                    novel.ontology. New locks apply forward-only.             [Py + LLM]
   ▼
6. DISPLAY SCAN     ONLY IF translated. A SECOND Aho-Corasick pass over the TRANSLATED
                    text, using target terms that can actually occur in the saved prose,
                    produces the mention spans the reader UI highlights. Independently
                    discovered display names are aligned to exact source substrings and
                    recorded as chapter-local terminology occurrences.   [Rust + Py + LLM]
   ▼
7. STATE EXTRACT    LLM pulls events + state changes for the ontology's tracked
                    attributes/relations (e.g. rank advance, faction destroyed,
                    ally→enemy), tagged with this chapter_index. BATCHED.    [Py + LLM]
   ▼
8. GRAPH WRITE      append-only upsert: entities, aliases (with first_seen_chapter),
                    facts/edges with BOTH source_chapter (=this chapter) and
                    valid_from_chapter (story-time; extractor infers it for flashbacks,
                    defaults to source_chapter otherwise); events; chunks + embeddings;
                    DISPLAY spans from step 6; set chapter.status = 'done'.    [Py + PG]
```

> **Source spans ≠ display spans (a real hole if you skip step 6).** Byte/char offsets
> computed against Chinese source text do not map onto the English translation — word order,
> entity position, even entity *count* change, and translation is not span-preserving. So
> highlighting English mentions with source offsets points at nonsense ranges (looks fine in
> code review, obviously broken only when rendered). Fix: reuse textproc for a second scan
> over the *translated* chapter with an automaton built from the glossary's locked
> `target_terms`. **Source-text spans are retained only for extraction provenance;
> display spans are produced by step 6.** (Rejected alternative: LLM-emitted inline sentinel
> markers — fragile and it breaks prefix caching.)
>
> A display span also needs a durable route back to the source spelling before a reader can
> safely confirm or correct its future translation. `term_rendering_occurrence` stores that
> chapter-local association: exact translated offsets, exact displayed text, and an exact
> substring copied from the source. Locked glossary matches are deterministic; otherwise an
> alignment call may propose the source substring, but validation drops invented, uncertain,
> or conflicting rows. This ledger is presentation/terminology data only— it never supplies
> an `entity_id`, and therefore does not weaken RESOLVE's sole ownership of identity (§0.3).
> Reader clients may overlay the currently visible glossary spelling onto these exact mapped
> occurrences, including earlier readable chapters. That is a reversible presentation view,
> not a rewrite of the immutable translated artifact: the stored prose and its provenance stay
> unchanged, while later TRANSLATE calls continue to consume the forward-only glossary lock.

Rules:
- The extraction prompts in steps 5 and 7 are **templated on `novel.ontology`** — they
  ask for that novel's kinds, attributes, and relations, nothing hardcoded.
- Steps 3–5 (terminology/extraction) run on the **source** text; step 6
  (display spans) runs on the **translated** text. Do not conflate the two span sets.
- Step 2 uses the glossary snapshot locked before this chapter began. Steps 3–5 may add
  terms only for future chapters. This forward-only boundary (§0.2) is deliberate: same-
  chapter enrichment is allowed to take minutes or fail, but reader-visible prose is not.
- Every LLM call is cache-checked first (§6) and, if batchable (steps 2 & 7), goes
  through the batch manager (§6) rather than calling the sync API.
- All writes are append-only and idempotent on `job.idempotency_key`.

STATE requests a JSON schema as well as JSON syntax. Unknown assertions are omitted:
explicit null/blank text in a fact, edge, entity or event discards that row with
diagnostics, never coerces it to a made-up string. Missing required keys, wrong
containers/types and invalid JSON still fail the stage. Valid rows survive an
unrelated unknown assertion. This validates structure, not factual correctness.

Reading readiness is separate from graph completion (`chapter.translation_ready`).
TRANSLATE is immediately after CHUNK so readers may open validated prose while enrichment
runs. After publishing prose, the worker atomically replaces the active queue pointer with
an `enrichment=true` pointer. Reader-critical untranslated chapters outrank those pointers,
so one chapter's graph work cannot delay the next chapter's translation. CHARACTER
NAMES/SCAN/RESOLVE never sit on the reader-critical path, and no partial graph is committed
without completed identity resolution. Enrichment
failures retain readable prose and receive up to three delayed retries, behind queued
reading work. Retries reuse the saved translation rather than rewriting it under newer
terminology. Missing assertions are not errors or placeholder facts: later evidence is
appended with its actual `source_chapter`, never backfilled into an earlier reader gate.

### Entity resolution — the part that silently fails at scale

Free-generating a canonical name per chapter drifts (same person, divergent names). The
fix is **retrieve-then-resolve**: the Rust scanner finds mentions, you fetch candidate
entities from the existing index, and the LLM only confirms or disambiguates against
those candidates. Build a small labeled eval set (a few hundred mentions) early and
track resolution accuracy as a CI metric — a drifting resolver corrupts every downstream
feature and you will not notice from the UI until chapter 600.

### 5.4 LLMProvider — the provider abstraction

All LLM calls go through this protocol. No pipeline stage imports a provider SDK
directly; backends are injected at startup. This is what lets you switch
Anthropic → DeepSeek → local Ollama with one env-var change.

```python
from enum import Enum
from typing import NotRequired, Protocol, TypedDict
from dataclasses import dataclass

class Class(Enum):
    """Priority class. Carried on EVERY call, from day one — see §14.2.
    Cheap to add now; a refactor of every call site later."""
    INTERACTIVE = "interactive"   # a reader is waiting (askai)
    BATCH       = "batch"         # offline ingestion (pipeline)

@dataclass
class Completion:
    """A completion AND the identity of what produced it.

    `served_model` is NOT decoration. It is what the LLM-result cache keys on
    (§3.5, §6.1). Returning a bare `str` here is the single change that makes
    a future gateway's failover silently corrupt the cache — see §12 and §14.3.
    Without a gateway, served_provider/served_model simply echo the request.
    """
    text: str
    served_provider: str
    served_model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0     # for cost accounting (workstream B) and §14.5
    cache_write_tokens: int = 0

class BatchRequest(TypedDict):
    id: str          # your idempotency key
    prompt: str
    system: str
    json_schema: NotRequired[dict | None]

class BatchResult(TypedDict):
    id: str
    output: str
    error: str | None
    served_provider: str
    served_model: str

class AdmissionRejected(Exception):
    """Backpressure, NOT job failure. Retry with backoff; must NOT increment
    job.attempts or the backlog dead-letters under load (§6.2, §14.3)."""
    retry_after_s: float = 0.0

class LLMProvider(Protocol):
    async def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        json_mode: bool = False,
        cls: Class = Class.BATCH,      # explicit at every call site
        pin_model: bool = False,       # forbid substitution — translate only (§14.3)
        json_schema: dict | None = None,
    ) -> Completion: ...

    async def embed(
        self,
        texts: list[str],
        *,
        cls: Class = Class.BATCH,
    ) -> list[list[float]]: ...

    # Offline batching — providers that lack native batching (DeepSeek, Ollama)
    # use the default implementation: runs complete() sequentially.
    # Anthropic overrides with the real Batch API (50% cost reduction).
    async def batch_submit(self, requests: list[BatchRequest]) -> str:
        """Returns a batch_id. Default: runs sequentially, returns a local UUID."""
        ...

    async def batch_poll(self, batch_id: str) -> list[BatchResult]:
        """Poll for completion. Default: immediately returns buffered results."""
        ...
```

Three details above exist only to keep a later gateway integration cheap, and all three
are near-free today (§14.2): the `cls` argument, the `Completion` return type carrying
`served_model`, and `AdmissionRejected` being its own exception. Retrofitting any of them
means touching every call site.

The optional `json_schema` is forwarded through the batch boundary. The Ollama
adapter passes it as native `format` with temperature zero. The current Anthropic,
DeepSeek and gateway adapters preserve it as guidance in the stable system prefix;
that fallback does not promise constrained decoding. All output is validated by
the calling stage regardless of backend.

**Embedding backend** is separate from the completion backend and can differ:
`nomic-embed-text` via Ollama is fast, local, and dimension-compatible with pgvector —
use it in dev regardless of which completion backend is active. In prod, swap to a
hosted embedding API if throughput demands it.

**Ask-AI uses the sync path only** (`complete()`). The Batch API is for offline
ingestion jobs. Never route interactive requests through the batch machinery.

**Pin the translation provider per novel; let extraction hop freely.** The glossary locks
*terms* but not *voice* — swap translation backends mid-novel and chapter 800 reads
differently from 799 even with identical terminology. This is a policy, not code: record
the chosen translation provider on the novel, warn on change, and keep it stable for the
run.

> **This stops being merely a policy the moment a gateway is involved.** A gateway that
> fails over on a 429 will substitute a different translation model *transparently*, which
> is the exact harm this paragraph forbids, arriving through a path no policy covers. That
> is why `complete()` takes `pin_model` (§5.4 above): **translate sets it, and nothing
> else does.** Extraction, resolution, and state-extract emit structured data, where a
> substituted model is a quality variance rather than a visible discontinuity in prose.
> See §14.3. Extraction/state/resolution can switch providers freely because their output is
structured data, not prose. The `chapter.translated_by` column is the provenance record
that also lets you find and selectively re-translate cheap-model chapters later.

Sibling to the ontology. Where the ontology adapts to *genre*, the language profile
adapts to *script*. Selected by `source_lang`; supplies the language-specific tooling the
mention scanner and chunker need:

```jsonc
{
  "lang": "ja",
  "segmenter": "lindera",        // word/sentence segmentation for no-space scripts
                                  // ja → lindera/sudachi, zh → jieba, en → whitespace
  "name_signals": {
    "honorifics": ["さん","様","先生","君","ちゃん"],  // strip for resolution; flag as person
    "script_hint": "katakana"     // katakana runs often mark names / foreign terms
  },
  "sentence_split": "ja"
}
```

Why it matters: Aho-Corasick matches exact byte substrings **without** segmentation — 王 is
found inside 王国 regardless (§3.3). Segmentation (jieba/lindera) is therefore **not** on the
match path; it is a *post-match boundary filter* that suppresses false positives where an
alias is a substring of a longer word (the surname 王 matched inside 王国 "kingdom"). That is
a disambiguation problem, not a matching one — do not wire a mandatory segmenter into the
matcher. Japanese honorifics must be stripped before resolution
(`田中さん` and `田中先生` are one entity) but are themselves a strong "this is a person"
signal, and katakana is a cheap proper-noun detector. The pipeline code is unchanged; it
reads the profile. Genre × language are independent: a Japanese cat romance is just
`genre: romance` + `lang: ja`.

### 5.2 Bulk backfill — processing a whole novel at once

Incremental (paste-as-you-read) and bulk (ingest an entire existing novel) are different
problems. Bulk has the whole book available, so it **breaks the in-order constraint** and
goes coarse-to-fine in two phases:

```
Phase 1 — HARVEST (cheap, parallel, any order)
  NER + Rust scanner over ALL chapters → candidate proper nouns
  cluster surface forms by co-occurrence → alias groups
  record first-appearance chapter per entity → first-mention map
  ⇒ warms the alias index before any LLM resolution runs

Phase 2 — ENRICH (LLM, ordered, index already warm)
  resolution confirms against the FULL candidate set (drift collapses)
  first-mention chapter      → full bio extraction (richest description lives here)
  every other chapter        → delta extraction (changes only)
  all batched (§6.2)
```

Payoffs:
- **Drift collapses** — Phase 2 resolution is never cold; every name is already a
  candidate, so the LLM confirms instead of discovering blind.
- **Cost drops** — full-bio extraction runs only on first-mention chapters (~one per
  entity), delta extraction everywhere else. A 4,000-chapter book introducing ~600
  entities pays for ~600 rich extractions, not 4,000.
- **Importance for free** — Phase-1 mention counts gate wiki richness: high-frequency
  entities get full bios + relationship graphs, rare ones get stubs.
- **Parallelism** — Phase 1 is order-free → fan out across KEDA workers (§13.2).

The §13.2 backfill `Job` runs Phase 1 then enqueues Phase-2 jobs.

### 5.3 Half-translated novels — bootstrap terminology from the existing translation

Common case: the reader is mid-way through a novel that has an existing fan translation
for chapters 1…M and raw source for M+1…N. Do **not** re-translate 1…M.

- Ingest the existing translation of 1…M directly as `translated_uri` (translate stage
  skipped); run extraction on it to populate the graph.
- **Harvest the glossary from that human translation** — whatever the translator called
  each sect / technique / character becomes the locked `target_term`.
- MT only the untranslated tail M+1…N, with that harvested glossary as hard constraints.

Result: the reader crosses the translation boundary and the names don't change under
them. Bootstrapping terminology from the existing translation is what makes the seam
invisible — it's the failure point of every naive MT continuation.

---

## 6. Caching & batching (offline cost control — first-class concern)

### 6.1 Caching layers

| Cache | Key | Backing | Invalidation |
|---|---|---|---|
| Raw fetch | `novel + chapter` | object store + `chapter.raw_uri` | never (immutable source) |
| LLM result | the canonical §3.5 key (`stage ‖ raw_hash ‖ prompt_version ‖ stage_config_version ‖ model_id`) | Redis → Postgres `job` | bump `prompt_version`; key changes automatically on backend switch |
| Embeddings | `sha256(text ‖ embed_model)` | Redis + pgvector | model change |
| Rendered read-path (hover card, wiki entry) | `(novel_id, entity_id, reader_chapter)` — see spoiler note below | Redis (TTL) | retro-update of involved terms |
| HTTP responses (scraper) | URL | Redis (short TTL) | manual |

The LLM-result cache is the big lever: re-processing a novel, replaying after a crash, or
re-running a stage you didn't change costs **zero** LLM spend because identical
`(stage, content, prompt_version, model_id)` tuples are served from cache. `model_id`
(provider + model) is non-optional in the key: omit it and a backend switch serves the
*previous* provider's cached output under a still-valid key — silent corruption.

**Hover-card cache must key on the authorization boundary — or it leaks spoilers *around*
RLS.** The rendered read-path cache key is `(novel_id, entity_id, reader_chapter)`. Do **not**
bucket chapters loosely: if a reader at ch.250 and one at ch.210 share a bucket, the ch.210
reader can be served a cached card containing facts with `source_chapter` 211–250 — a spoiler
leak *through the cache*, bypassing the DB gate. If buckets are used at all they must round
the reader's chapter **DOWN**, never up, so a cached card never contains facts newer than the
requesting reader. This is one of the two ways to see a spoiler around the row-level policy
(the other is event `entity_ids`, §4) — both belong in the flashback regression suite (§12).

### 6.2 Batching (the `BatchManager` in `pipeline`)

Translation and state-extraction are not latency-sensitive → run them through the
provider **Batch API** (Anthropic: ~50% cheaper). Ingestion being offline is exactly the
condition that lets cost-optimization machinery pay off.

```
collect():   pending translate/state jobs accrue into a buffer
flush():     trigger when buffer >= BATCH_MAX (e.g. 256) OR age >= BATCH_WINDOW (e.g. 5 min)
submit():    one provider batch; persist batch_id on each job; state→'batched'
poll():      poll batch status; on completion write results, state→'done'
recover():   on restart, re-poll in-flight batch_ids before submitting new work
```

- **Idempotency**: each request carries `job.idempotency_key`; results are matched back
  by it, so partial completion and retries never double-write.
- **Backpressure**: cap concurrent in-flight batches; pending jobs simply queue.
- **Dead-letter**: `attempts >= MAX_RETRY` → `state='deadletter'` for inspection; never
  block the pipeline on one poisoned chapter. **An `AdmissionRejected` is not an attempt.**
  Rate-limit backpressure must be retried with backoff *without* incrementing
  `job.attempts`, or the first sustained contention event dead-letters the whole backlog —
  a healthy rate limiter presenting as mass data loss (§14.3).
- **Admission (only when the gateway is enabled, §14)**: provider Batch APIs are metered
  separately from the sync APIs — Anthropic's Message Batches API is *exempt* from the
  Messages API rate limits and is instead capped on **batch requests in the processing
  queue**; OpenAI's batch pool likewise does not consume per-model limits and caps
  **queued prompt tokens**. So a batch submission is admitted against an outstanding-work
  semaphore, not a per-minute bucket, and the permit is held from `submit()` until the
  batch reaches a terminal state — up to 24h. Persist the gateway `reservation_id`
  alongside `batch_id` on the `job` row: the worker that polls completion is often not the
  process that submitted.
- The interactive Ask-AI path (§8) does **not** batch — it uses the sync API.
- **Off-peak scheduling (DeepSeek)**: DeepSeek *historically* discounted 50–75% during
  16:30–00:30 UTC for V3/R1, but **V4 off-peak pricing is unconfirmed** — treat it as
  opportunistic, not budgeted. When the active backend is DeepSeek you may still point the
  `BATCH_WINDOW` flush schedule at that window; just don't bank the savings.
- **Prefix caching differs by provider — this is the sharpest cost footgun.** Front-loading
  stable content (system instructions, ontology, glossary) *before* the per-chapter text is
  necessary for both, but sufficient for only one:
  - **DeepSeek**: caching is **fully automatic** (disk-based, on by default, 64-token minimum
    unit), and cached input drops ~**98%** ($0.14→$0.0028 per M on V4 Flash). Front-loading
    alone suffices.
  - **Anthropic**: caching is **NOT automatic**. The backend MUST place
    `cache_control: {"type": "ephemeral"}` on the last stable block (system + ontology +
    glossary). Nothing is cached without it and **no error is returned if you omit it** — the
    request silently runs uncached and your cost model breaks with no failing test. Respect
    the per-model minimum (1,024 tokens for Sonnet 4.6 / Opus 4.8, **4,096** for Haiku 4.5);
    prompts below it cannot be cached even when marked. Up to 4 breakpoints; cache-read 0.1×,
    5-min write 1.25×, 1-hr write 2× base input.
  - Either way, do not reorder chapter text ahead of the stable prefix — on DeepSeek it
    silently ~10×'s the input bill; on Anthropic it invalidates the marked prefix.

### 6.3 The reliable job queue (and the crash-recovery reaper — make it explicit)

The worker uses an atomic Lua claim to move a pointer from `jobs:pending` into
`jobs:processing` and record its start/heartbeat. This replaces plain FIFO `BLMOVE`:
within the oldest eligible novel it selects the lowest queued chapter number, unless
an explicit reader priority request overrides it. Active novels cannot be claimed by
another worker. Delayed enrichment retries yield to ordinary queued reading work.

- On claim, write `jobs:processing:started[raw_message] = now()` and a separate
  `jobs:processing:heartbeat` entry. The start timestamp remains the total chapter timer.
- Renew the heartbeat while inference is running. A **reaper** atomically recovers only
  claims with expired heartbeats, falling back to the start time for legacy claims.
  Old workers cannot acknowledge a newer claim. Slow live work is not treated as a crash.
- Completed pointers are discarded without repeating model calls. SIGTERM/SIGINT finish
  the current chapter and stop before claiming another. Operational failures are recorded
  separately from story data, without provider messages, credentials, or source text.
- KEDA scales on `LLEN jobs:pending` only, so an in-flight job (already in `processing`) stops
  counting toward desired replicas — fine for scale-to-zero, but set `minReplicaCount` /
  `cooldownPeriod` (§13.2) so a long in-flight job isn't killed mid-stage.

> **Alternative worth naming:** Redis **Streams + consumer groups** (`XREADGROUP`, `XPENDING`,
> `XAUTOCLAIM`) give consumer-group claim tracking and a pending-entries list — i.e. built-in
> visibility — for free. For a personal project the `BLMOVE` + reaper pattern above is
> acceptable; just make the reaper explicit rather than implied.

---

## 7. Source adapters (Go)

### 7.1 Paste (`ingest-api`)

`POST /novels/{id}/chapters` with `{ chapter_index, raw_text }` → build envelope →
enqueue. Paste mode has a free, powerful property: **future chapters don't exist in the
store**, so spoilers are structurally impossible and `current_chapter = max(ingested)`.
Ship this first.

### 7.2 Scraper (`scraper`)

Two strategies — implement next-link as primary, increment as fast-path:

- **Next-link following (primary, robust)**: fetch page → extract `<a rel="next">` /
  recognizable next button → follow. Immune to slug formats like
  `chapter-453-the-tribulation`; stops naturally when the link disappears.
- **URL increment (fast-path)**: only when the site has clean integer URLs
  (`url_template = .../chapter-{n}`). Enables parallel fetching.

**Extraction**: per-site CSS selector via goquery (one line per site, e.g.
`.chapter-content`) is highest quality and aggregators are structurally stable. Fallback
to a Python `trafilatura` micro-endpoint when the selector yields too little. Store raw
HTML **and** extracted text so you can re-extract later without re-fetching.

**Stop conditions** (don't trust status codes — aggregators serve 200s for "not found"):
stop when any two of → content length below a floor, content hash equals a prior chapter
(`textproc.HashContent`), or no next-link present.

**Politeness & resume** (or you get IP-banned mid-novel):
- rate-limit ≈ 1 req / few seconds with jitter, exponential backoff, real `User-Agent`,
  respect `robots.txt`.
- idempotent + resumable: persist each fetched chapter's `raw_hash`; skip what you have;
  a re-run continues exactly where it stopped.
- Cloudflare-fronted sites need a headless-browser (Playwright) fallback — note it,
  defer past MVP.

> Legal note (not legal advice): these aggregators host unlicensed fan translations;
> scraping them generally violates ToS and sits in murky copyright territory. For a
> personal reading tool that's a user decision. If this becomes a public portfolio demo,
> foreground paste-mode and licensed sources — not the scraper.

---

## 8. Reader API (Go) + Ask-AI (Python)

### 8.1 reader-api (Go) — the gate lives here

Auth → resolve `current_chapter` for the principal → every read endpoint filters
`<= current_chapter`. This app-layer filter is the outer of two layers; the inner layer
is Postgres RLS (§13.1), so a bug here still cannot leak future chapters.

```
GET  /novels/{id}/entity/{eid}?at={chapter}     # spoiler-safe bio (latest facts <= chapter)
GET  /novels/{id}/wiki?at={chapter}             # entities introduced <= chapter
GET  /novels/{id}/timeline?at={chapter}         # events <= chapter
GET  /novels/{id}/relationships/{eid}?at={chapter}
POST /novels/{id}/ask         { question, at }  # proxies to askai with the gate
PUT  /novels/{id}/progress    { chapter }       # advance current_chapter
```

### 8.2 askai (Python) — chapter-filtered RAG

```
retrieve():  vector search over chunk WHERE novel_id = $1 AND chapter_index <= $N
             (+ relevant facts/edges <= N). The LLM PHYSICALLY never sees future text.
generate():  sync LLM over retrieved context only; answer constrained to <= N.
```

The gate is enforced by filtering the index **before** retrieval, never by instructing
the model. This is the security property; treat it as non-negotiable.

---

## 9. Retro-update engine (Rust `textproc`)

When a better translation is chosen later, do not re-call the LLM on prior chapters.
Translations are stored with their `glossary_version`; a retro-update is a deterministic
canonical-term span replacement (`textproc.ApplyRetroUpdate`) that **writes a new object
version and repoints `chapter.translated_uri`** — never an in-place rewrite, consistent
with §0's append-only principle, so rollback is a pointer flip. Each change is recorded
in `glossary_changelog` (hash-chained for tamper evidence) and offered to the reader as
opt-in. Cheap, reversible, auditable.

---

## 10. Configuration (env)

```
# LLM provider — one of: anthropic | deepseek | ollama | gateway
LLM_PROVIDER=anthropic

# Completion models (provider-specific names; all "verify before run" — model IDs move weekly)
# anthropic:  claude-haiku-4-5 / claude-sonnet-4-6 / claude-opus-4-8 (Opus 4.7+ uses a newer
#             tokenizer that yields ~30% more tokens for the same text — budget accordingly)
# deepseek:   deepseek-v4-flash / deepseek-v4-pro  (PIN these — the deepseek-chat /
#             deepseek-reasoner aliases RETIRE 2026-07-24 15:59 UTC and then error;
#             an Anthropic-compatible endpoint also exists at https://api.deepseek.com/anthropic)
# ollama:     qwen3:8b / mistral  (qwen3 supersedes qwen2.5 for CJK; qwen2.5-coder still fine)
LLM_MODEL_TRANSLATE=claude-sonnet-4-6      # stronger model for translation
LLM_MODEL_EXTRACT=claude-haiku-4-5         # cheaper model for extraction / state

# Embedding model (can differ from completion backend)
# ollama:  nomic-embed-text (local, good for dev)
# hosted:  text-embedding-3-small or equivalent
EMBED_MODEL=nomic-embed-text
EMBED_DIM=768                              # MUST equal the VECTOR(768) entity+chunk columns
                                          # (§4) AND the model's real output. Assert at
                                          # startup: len(embed("probe")) == EMBED_DIM == 768.
                                          # A 768-vec into a 1024 col throws; there is no
                                          # silent coercion. Changing dim = re-embed migration.

# Ollama (only read when LLM_PROVIDER=ollama)
OLLAMA_BASE_URL=http://localhost:11434

# DeepSeek (only read when LLM_PROVIDER=deepseek)
DEEPSEEK_API_KEY=...
DEEPSEEK_BASE_URL=https://api.deepseek.com

# Anthropic (only read when LLM_PROVIDER=anthropic)
ANTHROPIC_API_KEY=...

# LLM gateway (only read when LLM_PROVIDER=gateway) — optional, see §14.
# The gateway proxies to the real backends; the models above still name what to ask for.
GATEWAY_GRPC_ADDR=llm-gateway:8080
GATEWAY_TENANT_MODE=novel_id               # novel_id | fixed — per-novel spend isolation
GATEWAY_UPSTREAM_PROVIDER=anthropic        # which backend the gateway should route to
GATEWAY_ON_UNAVAILABLE=direct              # direct | fail — fall back to calling the
                                           # provider directly if the gateway is down.
                                           # 'direct' keeps §14.1's "optional" promise
                                           # literally true; 'fail' is correct once the
                                           # gateway is the only thing preventing overrun.

PROMPT_VERSION=1                           # bump to invalidate LLM-result cache for all providers

# Batching (Anthropic only; other providers fall back to sequential)
BATCH_MAX=256
BATCH_WINDOW_SECONDS=300
MAX_RETRY=3
MAX_INFLIGHT_BATCHES=4

# Scraper
SCRAPE_RATE_PER_SEC=0.3
SCRAPE_JITTER_MS=800
SCRAPE_USER_AGENT="novel-engine/0.1 (+contact)"
CONTENT_LEN_FLOOR=800

# Infra
DATABASE_URL=postgres://...
REDIS_URL=redis://...
OBJECT_STORE_ENDPOINT=...                  # S3 (prod) or http://localhost:9000 (MinIO dev)
TEXTPROC_GRPC_ADDR=textproc:50051
```

---

## 11. Build order (MVP → polish)

**Milestone 1 — paste-mode MVP, one novel, prove the differentiator**
1. `db/migrations` + Postgres/pgvector/Redis/MinIO via docker-compose.
2. `ingest-api` paste endpoint + job enqueue.
3. `pipeline`: chunk → translate/readiness → (stub scan) → glossary resolve → state → display-scan
   (translated-text pass for UI spans, §5) → graph write, with LLM-result cache and
   BatchManager.
4. `textproc` Rust service: `ScanMentions` (Aho-Corasick) + `HashContent`.
5. `reader-api`: progress + spoiler-safe entity bio + wiki.
6. `askai`: chapter-filtered RAG.
7. `web`: reader pane, hover cards, Ask-AI box.

**Milestone 2 — scraping**
8. `scraper`: next-link following + per-site selector + politeness + resume + stop
   conditions; increment fast-path.

**Milestone 3 — polish**
9. Timeline + relationship diagrams, maps that unlock by chapter.
10. Retro-update engine + reader opt-in.
11. Multi-novel, power rankings, artifact/technique encyclopedias.

---

## 12. Known risks to watch

- **Knowledge-time vs story-time confusion** — the subtlest way to leak spoilers. Any
  new query, endpoint, or policy that gates on `valid_from_chapter` instead of
  `source_chapter` re-introduces the flashback-revelation leak (§0.3, §4). Add a
  regression test: a fact with `source_chapter=500, valid_from=10` must be invisible to
  a reader at 220 on every read path — asserted via RLS **alone** with the app filter
  removed, and in a fail-closed variant (no `SET LOCAL` → zero rows). Extend the SAME suite
  to the two ways a reader can see a spoiler *around* the row policy rather than through it:
  **(1)** an event visible at N whose `entity_ids` references an entity with
  `first_seen_chapter > N` (§4 event note — re-filter through gated entity visibility);
  **(2)** a hover-card cache bucket that spans chapters and serves a lower reader a
  higher reader's facts (§6.1 — key on `(novel_id, entity_id, reader_chapter)`, round down).
- **Entity-resolution drift** — the highest-impact silent failure. Eval set + accuracy
  metric from day one (§5).
- **LLM cost** — mitigated by aggressive caching + Batch API (Anthropic, 50% off in *and*
  out) or switching to DeepSeek for bulk runs. Monitor spend per chapter. DeepSeek has no
  *synchronous* Batch API, but automatic disk prefix caching (~**98%** off cached input on
  V4 Flash) recovers most of it; off-peak scheduling (16:30–00:30 UTC) is historically
  50–75% off but **unconfirmed for V4** — don't budget it (§6.2). Anthropic prompt caching
  is **not automatic** — it needs explicit `cache_control` and per-model minimums, or it
  silently runs uncached (§6.2). Don't over-index on "DeepSeek loses batching."
- **Cache key must include `model_id`** — the LLM-result / idempotency key gates on
  `model_id` (§4 `job`, §6.1); drop it and a backend switch serves the previous provider's
  cached output under a valid key. Same class of missing-dependency bug the design
  otherwise guards against — assert it in a test.
- **`model_id` must be the model that ANSWERED, not the one you asked for.** The bug above
  has a second door, and it only opens once a gateway or router sits in the path: on a 429
  the gateway fails over to another model, returns a perfectly good completion, and the
  pipeline caches it under the *requested* `model_id`. Every later cache hit then serves
  another model's output under a key asserting it came from this one — the same silent
  corruption, arriving through resilience machinery rather than a config change. The fix is
  structural, not vigilance: `complete()` returns a `Completion` carrying
  `served_provider`/`served_model` (§5.4), and the cache keys on those. Assert it with a
  test where the provider deliberately reports a different served model than requested and
  the key changes. (§14.3)
- **Translation voice drift across a provider switch** — the glossary locks terms, not
  prose style, so changing the *translation* provider mid-novel makes chapter 800 read
  unlike 799. Pin the translation provider per novel and warn on change (§5.4);
  `chapter.translated_by` records what produced each chapter.
- **DeepSeek data routing** — translation content goes to Chinese servers. For personal
  use this is a you-decision; flag it if the system ever handles sensitive or licensed
  content.
- **Scraper bans / anti-bot** — politeness + resume; Playwright fallback only if forced.
- **Glossary lock-in too early** — a bad early canonical choice propagates; the
  retro-update path (§9) is the escape hatch, so build it before scaling chapter count.

---

## 13. Deployment & operations

**Local dev first, cloud when it earns it.** The `docker-compose.yml` (Postgres,
pgvector, Redis, MinIO, all services) is the real development environment. The EKS/RDS
section below is the production target for when you want a live demo or a resume-facing
deployment — not a day-one requirement. Run everything in docker-compose until the
pipeline works end-to-end on one novel, then lift.

Deployed on **EKS** with stateful services managed (**RDS** Postgres, **ElastiCache**
Redis, **S3**), everything provisioned via **Terraform**. The principle mirrors §0: add
infra only where a real property of this system demands it — bursty offline load, a
polyglot fleet, variable LLM cost, and an authorization gate at the core. Each subsection
below is motivated by one of those, not by checklist completeness.

### 13.1 Defense-in-depth chapter gate — Postgres Row-Level Security

The spoiler gate is the core security property, so it is enforced at **two** layers: the
app filter (§8) *and* the database. RLS makes the DB itself refuse to return rows above
the reader's progress, so a single app-layer bug cannot leak protected content.

Three rules the policies must encode (each closes a real leak):
1. **Gate on `source_chapter`** (knowledge-time), never `valid_from_chapter` (story-time)
   — see the flashback-revelation note in §4.
2. **Scope by novel** — a reader is at ch.800 of novel A and ch.5 of novel B; one global
   chapter number would leak B.
3. **Cover `entity` and `alias` too** — wiki introductions and identity-revealing
   epithets are as spoiler-sensitive as facts.

```sql
-- Helper: fail CLOSED if the GUC was never set (current_setting on an unset GUC throws;
-- the (…, true) form returns NULL, and COALESCE(-1) then matches nothing).
CREATE FUNCTION reader_chapter() RETURNS int LANGUAGE sql STABLE AS
  $$ SELECT COALESCE(NULLIF(current_setting('app.current_chapter', true), '')::int, -1) $$;
CREATE FUNCTION reader_novel() RETURNS uuid LANGUAGE sql STABLE AS
  $$ SELECT NULLIF(current_setting('app.novel_id', true), '')::uuid $$;

ALTER TABLE fact   ENABLE ROW LEVEL SECURITY;
ALTER TABLE edge   ENABLE ROW LEVEL SECURITY;
ALTER TABLE event  ENABLE ROW LEVEL SECURITY;
ALTER TABLE chunk  ENABLE ROW LEVEL SECURITY;
ALTER TABLE entity ENABLE ROW LEVEL SECURITY;
ALTER TABLE alias  ENABLE ROW LEVEL SECURITY;
-- Owners bypass RLS by default — FORCE closes that hole if the app user owns the tables.
ALTER TABLE fact   FORCE ROW LEVEL SECURITY;
ALTER TABLE edge   FORCE ROW LEVEL SECURITY;
ALTER TABLE event  FORCE ROW LEVEL SECURITY;
ALTER TABLE chunk  FORCE ROW LEVEL SECURITY;
ALTER TABLE entity FORCE ROW LEVEL SECURITY;
ALTER TABLE alias  FORCE ROW LEVEL SECURITY;

-- PERFORMANCE FOOTGUN: a STABLE function called bare in USING() is evaluated as a per-row
-- SubPlan (→ seq scan). WRAP every call in a scalar subquery — (SELECT reader_chapter()) —
-- so Postgres evaluates it ONCE as an InitPlan and caches it for all rows. Also index the
-- policy columns (source_chapter, first_seen_chapter, chapter_index, novel_id).
CREATE POLICY gate_fact  ON fact
  USING (novel_id = (SELECT reader_novel()) AND source_chapter     <= (SELECT reader_chapter()));
CREATE POLICY gate_edge  ON edge
  USING (novel_id = (SELECT reader_novel()) AND source_chapter     <= (SELECT reader_chapter()));
CREATE POLICY gate_event ON event
  USING (novel_id = (SELECT reader_novel()) AND chapter_index      <= (SELECT reader_chapter()));
CREATE POLICY gate_chunk ON chunk
  USING (novel_id = (SELECT reader_novel()) AND chapter_index      <= (SELECT reader_chapter()));
CREATE POLICY gate_entity ON entity
  USING (novel_id = (SELECT reader_novel()) AND first_seen_chapter <= (SELECT reader_chapter()));
CREATE POLICY gate_alias ON alias
  USING (first_seen_chapter <= (SELECT reader_chapter())
         AND entity_id IN (SELECT id FROM entity));  -- novel scope via gated entity (nested RLS
                                                     -- scopes the inner query). If `alias` ever
                                                     -- grows large, replace this correlated
                                                     -- IN(subquery) with a SECURITY DEFINER STABLE
                                                     -- helper returning the allowed entity-id set,
                                                     -- wrapped as (SELECT allowed_entities()).

-- Two roles: readers are gated, the ingestion writer bypasses RLS (writes ALL chapters).
CREATE ROLE rls_reader    NOLOGIN;
CREATE ROLE ingest_writer NOLOGIN BYPASSRLS;
```

**Wiring (four hard requirements — break any one and the gate silently no-ops):**
`reader-api` **and** `askai` connect as `rls_reader` (askai's vector search hits the
RLS-gated `chunk` table, so it needs the same GUCs). The `pipeline` service connects as
`ingest_writer`. Then, per request:

1. **Always inside an explicit transaction.** `SET LOCAL` is a **no-op with a warning
   outside a transaction**, leaving the GUC unset → fail-closed COALESCE(−1) → the query
   silently returns zero rows. Every reader-api/askai request MUST `BEGIN`, set the GUCs,
   run its query, and `COMMIT` in the *same* transaction (a single request-scoped dependency
   in FastAPI; the same tx in Go pgx).
2. **Set via bound parameter, not string interpolation.** Prefer
   `SELECT set_config('app.current_chapter', $1, true)` (and `app.novel_id`) over
   interpolated `SET LOCAL` — injection-safe and clean over the driver protocol. In
   transaction-pool mode, asyncpg/pgx may need server-side prepared statements disabled.
3. **pgbouncer `pool_mode` must be `transaction` or `session`, never `statement`** —
   statement mode breaks `SET LOCAL`/`set_config(local)` entirely.
4. **Never set the GUC on a pooled connection outside its transaction** — it would leak into
   the next borrower of that connection.

A forgotten setting fails **closed** (helper returns −1 → zero rows), never open. Note
`valid_from_chapter` intentionally does NOT appear in any policy — it is display logic (§4
key queries), not authorization.

### 13.2 Autoscaling the offline plane — KEDA, scale-to-zero on queue depth

Pipeline load is bursty: a backfill floods the queue, then it sits idle for hours. KEDA
scales the Python workers on Redis queue depth and scales them **to zero** when drained —
the cost story that generic HPA-on-CPU can't tell. It scales on `LLEN jobs:pending`; the
`jobs:processing` reliable-queue + reaper (crash recovery via a visibility timeout) is
specified in §6.3, and `minReplicaCount`/`cooldownPeriod` below keep a long in-flight job
from being killed mid-stage.

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata: { name: pipeline-workers }
spec:
  scaleTargetRef: { name: pipeline }     # the Deployment
  minReplicaCount: 0                      # scale to zero when idle
  maxReplicaCount: 20
  cooldownPeriod: 300
  triggers:
    - type: redis
      metadata:
        address: redis:6379
        listName: "jobs:pending"
        listLength: "20"                  # ≈ one replica per 20 queued jobs
```

Three K8s workload types, each used for the right reason:
- **Deployment + KEDA** — the pipeline workers above.
- **CronJob** — nightly scrape polls (`schedule: "0 * * * *"`, `args: ["poll"]`).
- **Job** — one-shot backfills of a novel's full archive.

### 13.3 GitOps + supply-chain-hardened CI

Polyglot makes the CI genuinely interesting: a matrix build across Rust / Go / Python /
TS. The security differentiator is supply-chain control, not just "tests pass":

- Build + test matrix per service.
- `trivy` image + dependency scan (fail on high/critical).
- `syft` → SBOM artifact per image.
- `cosign` signs every image; images pushed to ECR.
- **ArgoCD** (or Flux) syncs the cluster from the repo — Git is the source of truth.
- Cluster admission policy (Kyverno / cosign policy-controller) **rejects unsigned
  images**, so only CI-built, signed artifacts can run.

### 13.4 Observability — domain + cost metrics, not CPU graphs

Prometheus + Grafana, but the panels that matter are domain- and cost-specific (most
projects can't show these because they have no real variable cost):

- **LLM cost per 1,000 chapters** (with budget alerts), cache hit rate, batch latency.
- Queue depth, scrape success rate, dead-letter count.
- **Entity-resolution accuracy** against the §5 eval set, tracked over time.

Add **OpenTelemetry** tracing: a single ingest flows Go → Python → Rust, so one
cross-language distributed trace is a strong, hard-to-fake demo of the whole path.

### 13.5 Network policy + secrets

- **Scraper egress lockdown** — a `NetworkPolicy` lets the scraper reach the public
  internet (443) plus Redis/Postgres, and **nothing else internal**. Egress is the thing
  worth restricting on a component that talks to untrusted sites.
- **reader-api ingress** — accept traffic only from the ingress controller.
- **Postgres / LLM reachability** — restrict to the services that need them.
- **Secrets** — pull LLM keys via **External Secrets Operator** from AWS Secrets Manager;
  no raw `Secret` objects or keys in manifests.

### 13.6 Workload → Kubernetes mapping

| Component | Workload | Scaling |
|---|---|---|
| reader-api (Go) | Deployment | HPA on RPS, min 2 |
| askai (Py) | Deployment | HPA on RPS |
| pipeline (Py) | Deployment | **KEDA on queue depth, min 0** |
| textproc (Rust) | Deployment | HPA on CPU |
| scraper (Go) | **CronJob** (poll) + **Job** (backfill) | — |
| Postgres / Redis / blobs | RDS / ElastiCache / S3 (managed) | managed |

### 13.7 What was deliberately NOT added

A service mesh (theater at this scale), Kafka (Redis is the correct queue here), and
multi-region (no requirement). Depth on the above beats breadth across a dozen CNCF
logos — a project that does KEDA scale-to-zero, DB-enforced authz, and signed-image
GitOps *well* reads as someone who has operated systems.

---

## 14. LLM gateway integration (the `llm-inference-gateway` sibling project)

`llm-inference-gateway` (`~/projects/llm-inference-gateway`) is a Go service that sits
between clients and LLM backends and decides what is admitted, in what order, and to
which backend. This engine is its reference client. Its spec §15 is the same contract
from the server side; **that document is authoritative for gateway behavior, this section
is authoritative for what this codebase does.**

### 14.1 It is optional, and stays optional

The gateway is one more `LLMProvider` implementation (§5.4), selected by
`LLM_PROVIDER=gateway`. **This system must run, demo, and pass its full test suite with
the gateway switched off.** Every test that exercises an LLM path runs against a direct
provider; the gateway adds a parallel CI job, never a prerequisite. Separate repos, no
shared code — the contract is the gateway's pinned `gateway.proto`, from which the Python
client is generated.

Why hold this line: the two projects stand alone as separate pieces of work. Making the
translation engine unrunnable without a rate limiter running trades a working demo for an
architecture diagram.

### 14.2 What to build during Phase 1, before any gateway exists

Three changes, roughly a day inside Phase 1, that turn the eventual integration into
"write one new provider class" instead of "touch every LLM call site." All three are also
independently defensible without a gateway, which is why they are not speculative work:

1. **Carry a priority `Class` on every `LLMProvider` call.** `askai` passes
   `INTERACTIVE`, `pipeline` passes `BATCH`. Without a gateway it is an unused argument
   that documents intent; with one it is the whole scheduling signal.
2. **Return `Completion`, not `str`** (§5.4) — carrying `served_provider`/`served_model`
   and token counts. Key the LLM-result cache on the *served* values (§12). The token
   counts feed cost accounting (PLAN workstream B) on day one regardless.
3. **Make `AdmissionRejected` a distinct exception** from processing failure, and make the
   worker retry it with backoff without incrementing `job.attempts` (§6.2, §14.3).

### 14.3 The three silent failure modes at this seam

**(1) Failover corrupts the LLM-result cache.** Covered in §12 and fixed by keying on
`served_model`. This is the one most likely to be missed, because the corrupted output is
*correct-looking text* — nothing crashes and no test fails.

**(2) Failover changes translation voice.** The glossary locks terms, not prose style
(§5.4). Set `pin_model=True` on translate and nothing else; the gateway then rejects
rather than substituting. A rejected translate job requeues, which is strictly better than
a chapter that reads differently from its neighbors.

**(3) Backpressure gets dead-lettered.** `RESOURCE_EXHAUSTED` is the gateway working, not
the job failing. See §6.2.

### 14.4 Workload → priority class

| Call site | Class | Backend | Pinned? |
|---|---|---|---|
| `pipeline` resolve | BATCH | hosted sync | no |
| `pipeline` translate | BATCH | hosted **batch API** | **yes** |
| `pipeline` state-extract | BATCH | hosted **batch API** | no |
| `pipeline` chunk embedding | BATCH | local GPU (Ollama) | n/a |
| `askai` question embedding | **INTERACTIVE** | local GPU (Ollama) | n/a |
| `askai` answer generation | **INTERACTIVE** | hosted sync | no |
| schema induction (§4.2) | BATCH | hosted sync | no |

The two embedding rows are why the gateway bothers to gate embeddings at all: they are the
same local model serving opposite latency requirements. A backfill embedding tens of
thousands of chunks and a reader waiting on one question embedding hit the same Ollama,
and **Ollama has no request priority** — it serves FIFO up to `OLLAMA_MAX_QUEUE` (default
512) and then returns 503. So the reader genuinely does wait behind the backfill, and the
only place to fix it is in front of Ollama. This engine supplies that demonstration; the
gateway supplies the fix.

### 14.5 Operational interactions

- **Tenancy = `novel_id`.** The gateway's quota keys are `(tenant, provider, model)`, so
  making the tenant a novel gives per-novel spend isolation and per-novel cost dashboards
  (§13.4) for free, and stops one novel's backfill starving another's readers.
- **Separate Redis logical DBs.** This system's queue/cache and the gateway's quota state
  must not share an eviction domain — a backfill filling Redis must not evict quota state.
- **Timeouts must nest**: reaper visibility timeout (§6.3) **>** gateway lease timeout
  **>** single request duration. Inverted, a job is requeued while its reservation is
  still held and the same chapter is processed twice. Assert the ordering at startup.
- **KEDA and admission are complements** (§13.2). Scaling workers on `LLEN jobs:pending`
  scales on *demand*; nothing in that loop knows the provider's capacity, so 20 workers
  will cheerfully saturate a limit sized for 3. Queue-depth autoscaling without central
  admission control is over-provisioning with extra steps — worth saying plainly, since
  the two features justify each other.
- **Prefix caching interacts with metering.** Front-loading system + ontology + glossary
  (§6.2) means most input tokens are cache *reads* — and on current Anthropic models cache
  reads **do not count toward the input rate limit** at all. The gateway must be told the
  cached portion separately or it will meter this workload at several times its true cost.
  That is what `Completion.cache_read_tokens` reports back, and it is also the number
  §13.4's cost panel needs.

### 14.6 Where this sits in the build order

Summary; the gateway spec §15.6 has the full joint table.

1. Phases 1–3 here (the vertical slice), direct providers, **plus §14.2's seam prep**.
2. Gateway Phases 0–2.5, built against its own mocks.
3. Phase 4 (textproc) and Phase 5 (web) here — independent, can overlap.
4. Gateway points at real Ollama, then this engine switches to `LLM_PROVIDER=gateway`.
5. **Milestone 3.3 (bulk backfill) and the gateway's leasing optimization together** —
   backfill is the first workload that creates real batch-scale spend and sustained
   contention, which is the only condition under which either is measurable rather than
   asserted.

The slice never blocks on the gateway; the gateway is never designed against a
hypothetical client.


### Glossary deletion (local management extension, migration 0019)

Human deletion sets `glossary.deleted`, increments the novel-wide glossary version,
and appends the existing hash-chain payload with `new_target=""` as a deletion
marker. The stored target and audit history are retained; existing prose and graph
facts are not rewritten. Active glossary reads exclude tombstones, but version
calculation includes them so deleting the last term still invalidates translation
cache inputs. RESOLVE must not automatically restore a tombstone. Explicit human
creation through bootstrap can restore it with a fresh version. The unique target
index covers only active rows. Glossary listing keeps its chapter gate; readers
with no progress can see only human seed terms locked at chapter zero.

### Pipeline reliability repair (migration 0029)

Durable stage completion is chapter-owned: the uniqueness and all completion checks use
`(novel_id, chapter_index, stage, idempotency_key)`. Response caches remain
content-addressed and may be shared only when their complete inputs match. Translation
cache identity includes both languages, ontology, the complete ordered glossary, prompt
version and served model.

A non-empty translation that still misses a locked term after one protected
`<locked-term>` retry remains readable. The chapter stores only
`locked_terms_missing` and a count, never term values or prose; that warning output is
not cached and enrichment continues. Empty output, provider errors, malformed protected
markup and persistence errors remain hard failures. Graph work retries after 5, 15 and
45 minutes, then chronologically blocks later chapters until explicit operator resume.

Managed entity resolution is occurrence-local and revision-scoped. Candidate retrieval
uses exact alias/canonical matches first and then filtered pgvector cosine neighbors,
limited to eight total candidates of the same ontology kind from earlier chapters. Model
answers may select only IDs offered for that occurrence. Source discovery, proposal,
verification and display alignment use bounded passage/windows targeting 36 KiB and
never cross the 42 KiB hard request ceiling. Name discovery's wire contract is one flat,
64-row `names` array plus an explicit all-kinds `reviewed` object; application aggregation
across bounded requests has no chapter-global 64-name limit.

Only verified source/display alignments published by an active trusted revision vote in
the global distinct-chapter terminology ledger. Two chapters agreeing exactly promote a
managed glossary row with `entity_id NULL`; revision identity lives in
`glossary_binding`. Staging work cannot vote. Human wording, deletion, target uniqueness,
version order and the changelog hash chain remain authoritative; automatic collisions
stay unapplied instead of failing the chapter.

Legacy matching accepts only verbatim, ontology-typed proposed surfaces, applies whole
word boundaries outside CJK, contextually disambiguates one-character CJK aliases and
requires repeated occurrences to agree before binding a surface. Vector candidates are
kind-filtered and embedding cardinality is checked. Before legacy graph writes, facts,
edges and events must reference declared, resolved ontology-valid entities; bad rows are
logged and discarded individually, while an event with any unresolved participant is
discarded whole. Reader chapter/list responses expose the operational translation
warning, and warning chapters remain Ready rather than Failed.

## 15. Private hosted accounts (September 2026)

The hosted release is invite-only with Google authentication. Each novel has exactly one
account owner; no sharing exists. Ownership authorization and §0.3 chapter clearance
are independent, mandatory constraints. API keys, embedding choices, progress, caches,
and queue controls are account-scoped. Hosted code must not fall back to server model
credentials. Approved custom endpoints must be public HTTPS and checked on actual calls.

The initial deployment supersedes the §13 EKS target: one EC2/k3s node, single-AZ RDS
PostgreSQL, S3, persistent Redis, and automated restore-tested backups. Maintenance
windows are acceptable. No SQS or gateway dependency is introduced. Application
interfaces remain portable to ordinary PostgreSQL, Redis and S3-compatible storage.
Google identity uses issuer/subject, not email as a permanent identity. Sessions and
invitations are revocable; deleting an account stops work and durably cleans objects.
