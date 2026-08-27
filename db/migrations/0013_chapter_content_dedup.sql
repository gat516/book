-- 0013_chapter_content_dedup.sql — content-addressed chapter deduplication.
-- Forward-only.
--
-- instructions.md §0 already declares ingestion "offline + idempotent, content-hash
-- keyed", and `chapter.raw_hash` (0001) has held the sha256 of every chapter body since
-- the beginning — but nothing ever keyed on it. `chapter`'s only uniqueness was
-- (novel_id, chapter_index), and the scraper assigns a FRESH chapter_index (MAX+1) at the
-- start of each job, so re-running a scrape over pages already ingested inserted a second
-- copy of every chapter under new indices without conflicting on anything. Observed for
-- real: a re-run added 27 duplicate chapters to one novel.
--
-- This index closes that hole at the database level; ingest-api additionally checks the
-- hash before writing so the common case returns a clean "duplicate" response rather than
-- relying on a constraint violation (defense in depth, the same posture RLS + app-layer
-- WHERE clauses take on the read side).
--
-- Deliberately scoped per novel, not global: two different novels legitimately may share
-- an identical short chapter, and cross-novel collision is not this constraint's business.
BEGIN;

CREATE UNIQUE INDEX chapter_novel_raw_hash_key ON chapter (novel_id, raw_hash);

COMMIT;
