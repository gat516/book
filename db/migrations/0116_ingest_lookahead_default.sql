-- 0116_ingest_lookahead_default.sql -- fetch a 10-chapter buffer ahead of the reader.
--
-- ingest_lookahead is how far ahead of the furthest reader the scraper may fetch. It
-- defaulted to 50, which is a lot of a stranger's bandwidth spent on chapters nobody has
-- asked for yet, and at the paced fetch rate (scraper/politeness.go) it is hours of
-- requests. Ten is enough that a reader never waits: the scraper fetches the page a
-- caught-up reader is waiting on promptly and then rebuilds the buffer at the polite
-- pace, far faster than chapters are read.
--
-- Existing books keep whatever they were set to; this is the default for new ones.
BEGIN;

ALTER TABLE novel ALTER COLUMN ingest_lookahead SET DEFAULT 10;

COMMIT;
