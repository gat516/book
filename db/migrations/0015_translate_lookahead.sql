-- 0015_translate_lookahead.sql — how far ahead of the reader to translate. Forward-only.
--
-- Ingestion and translation have wildly different costs: fetching a chapter is one HTTP
-- request, translating it is a dozen-plus sequential LLM calls. Until now ingest-api
-- enqueued a pipeline job for EVERY chapter it accepted, so scraping a novel queued the
-- whole novel for translation — measured here, 145 chapters fetched in 30 minutes with
-- none finished, because the worker was grinding through chapters nobody was reading.
--
-- Translation becomes demand-driven instead: chapters are ingested freely, but only those
-- within translate_lookahead of the reader's position are queued. Per novel because the
-- right depth depends on the book and on how far ahead the reader wants to run.
--
-- 0 means "translate nothing automatically" — only chapters explicitly requested.
BEGIN;

ALTER TABLE novel ADD COLUMN translate_lookahead INT NOT NULL DEFAULT 5;

COMMENT ON COLUMN novel.translate_lookahead IS
  'How many chapters past the reader''s position to keep translated. 0 = on demand only.';

COMMIT;
