-- 0068_completion_cache.sql — a cross-revision, novel-scoped completion cache (§0.7).
--
-- Why a NEW table, not ALTER graph_completion: 0052 (lines 82-84) put a composite FK from
-- graph_completion_run onto graph_completion's 4-column PK (revision_id,cache_key,
-- served_provider,served_model). That FK pins the PK; it cannot be widened or have a column
-- dropped in place. graph_completion and graph_completion_run are left intact and read-only
-- (existing rows stay queryable; benchmark_knowledge.py keeps writing to graph_completion
-- directly, unrelated to KnowledgeEngine's production cache path).
--
-- Why novel-scoped, not global: completion_cache.response holds extracted source quotes.
-- A global cache would outlive `DELETE FROM novel` (0030 made every dependent FK CASCADE so
-- that single statement is a complete purge) and leak a deleted book's text into a cache row
-- keyed by content hash alone. novel_id FK ON DELETE CASCADE keeps the purge property intact.
--
-- Why the cache key no longer includes revision_id (see pipeline/knowledge.py's KnowledgeEngine.call):
-- every graph_revision.id was a fresh cold cache under the old scheme, which is why 670 calls
-- were spent extracting one chapter 17 times over. completion_cache_run below carries
-- per-revision-per-run attribution instead, deliberately WITHOUT a foreign key back to
-- completion_cache's PK: run bookkeeping must never be able to fail an extraction that already
-- has (or will have) a valid cache entry.
BEGIN;

CREATE TABLE completion_cache (
  novel_id uuid NOT NULL REFERENCES novel(id) ON DELETE CASCADE,
  cache_key text NOT NULL,
  served_provider text NOT NULL
    CHECK (served_provider = ANY (ARRAY['anthropic'::text,'deepseek'::text,'gemini'::text,'ollama'::text])),
  served_model text NOT NULL,
  response jsonb NOT NULL,
  elapsed_seconds double precision,
  runtime_metrics jsonb NOT NULL DEFAULT '{}',
  stage text,
  chapter_index int,
  created_at timestamptz NOT NULL DEFAULT now(),
  last_used_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (novel_id, cache_key, served_provider, served_model)
);

-- Per-run attribution only. No FK to completion_cache's PK (see header). chapter_index/stage/
-- batch_id are NOT NULL here (unlike the nullable-for-migration-compat columns 0052 bolted onto
-- graph_completion) because every completion_cache_run row is written fresh by the current
-- KnowledgeEngine, never backfilled onto pre-existing rows.
CREATE TABLE completion_cache_run (
  revision_id uuid NOT NULL REFERENCES graph_revision(id) ON DELETE CASCADE,
  cache_key text NOT NULL,
  served_provider text NOT NULL,
  served_model text NOT NULL,
  run_id uuid NOT NULL REFERENCES chapter_knowledge_run(id) ON DELETE CASCADE,
  chapter_index int NOT NULL,
  stage text NOT NULL,
  batch_id text NOT NULL,
  PRIMARY KEY (revision_id, cache_key, served_provider, served_model, run_id)
);

GRANT SELECT, INSERT, UPDATE ON completion_cache TO ingest_writer;
GRANT SELECT, INSERT ON completion_cache_run TO ingest_writer;

-- graph_completion's CHECK is a landmine even once nothing writes it anymore: leaving it
-- 'ollama'-only would still block any future ad-hoc read/report code that tries to insert a
-- hosted row there. Mirrors 0040_event_completion_providers.sql's wording exactly.
ALTER TABLE graph_completion DROP CONSTRAINT graph_completion_served_provider_check;
ALTER TABLE graph_completion ADD CONSTRAINT graph_completion_served_provider_check
  CHECK (served_provider = ANY (ARRAY['anthropic'::text,'deepseek'::text,'gemini'::text,'ollama'::text]));

COMMIT;
