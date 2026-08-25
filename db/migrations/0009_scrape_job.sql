-- 0009_scrape_job.sql — scraper job tracking, so a web-triggered scrape (PLAN.md Phase
-- N5) is observable and cancellable from outside the scraper process, mirroring the
-- pipeline's own jobs:pending/jobs:processing + chapter.status pattern (worker.py).
-- Forward-only.
--
-- No RLS/spoiler gate needed: this is operational metadata about an ingestion run (a
-- novel_id, a URL, a status), not story content — nothing here has a chapter_index or
-- source_chapter to gate on.
BEGIN;

CREATE TABLE scrape_job (
  id                BIGSERIAL PRIMARY KEY,
  novel_id          UUID NOT NULL REFERENCES novel(id),
  start_url         TEXT NOT NULL,
  -- bootstrap: fetched text has no separate original (e.g. an already-translated site) —
  -- stored as BOTH raw_text and translated_text, so TranslateStage's early-out skips the
  -- LLM call (PLAN.md N5/N6). translate: fetched text is source-language; the pipeline
  -- machine-translates it as usual.
  mode              TEXT NOT NULL DEFAULT 'translate',
  status            TEXT NOT NULL DEFAULT 'pending', -- pending|running|done|error|cancelled
  chapters_fetched  INT NOT NULL DEFAULT 0,
  last_error        TEXT,
  cancel_requested  BOOLEAN NOT NULL DEFAULT false,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (mode IN ('bootstrap', 'translate')),
  CHECK (status IN ('pending', 'running', 'done', 'error', 'cancelled'))
);
CREATE INDEX ON scrape_job (novel_id, created_at DESC);

-- Only one active scrape per novel at a time — chapter_index continuity (assigned as
-- MAX(chapter_index)+1 when a job starts) is only correct if two jobs never race.
CREATE UNIQUE INDEX scrape_job_one_active_per_novel ON scrape_job (novel_id)
  WHERE status IN ('pending', 'running');

GRANT SELECT ON scrape_job TO rls_reader;

-- reader-api creates jobs and sets cancel_requested through its progressDB pool
-- (reader_progress_writer — the same role that already owns writing reader_progress).
GRANT SELECT, INSERT ON scrape_job TO reader_progress_writer;
GRANT UPDATE (cancel_requested) ON scrape_job TO reader_progress_writer;
GRANT USAGE, SELECT ON SEQUENCE scrape_job_id_seq TO reader_progress_writer;

COMMIT;
