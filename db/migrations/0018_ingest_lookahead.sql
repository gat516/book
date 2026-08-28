-- 0018_ingest_lookahead.sql — how far ahead of the reader to FETCH, as distinct from how
-- far ahead to TRANSLATE. Forward-only.
--
-- The two windows are deliberately separate because the work behind them differs by orders
-- of magnitude. Fetching a chapter is one HTTP request; translating it is a dozen-plus
-- sequential LLM calls. So a novel wants to run a wide ingest window (cheap, and content
-- is worth having locally before you need it) over a narrow translate window (expensive,
-- and only useful just ahead of where you're reading).
--
--   ingest_lookahead    (this column)  — stop FETCHING once this many chapters ahead
--   translate_lookahead (0015)         — keep this many chapters TRANSLATED ahead
--
-- Measured against the reader's position rather than queue depth: since the scraper stopped
-- queueing translation for what it ingests, the pipeline queue sits near empty and a
-- depth-based limit never engages. "Ahead of the reader" is the thing actually worth
-- bounding — it is what stops a 5000-chapter serial being pulled down in one sitting.
--
-- 0 means unlimited (fetch to the end of the novel).
BEGIN;

ALTER TABLE novel ADD COLUMN ingest_lookahead INT NOT NULL DEFAULT 50;

COMMENT ON COLUMN novel.ingest_lookahead IS
  'Stop fetching once this many chapters are ingested beyond the reader''s position. 0 = unlimited.';

COMMIT;
