-- 0014_scrape_job_max_queue_depth.sql — per-novel limit on how far a scrape may run
-- ahead of the pipeline. Forward-only.
--
-- Fetching is network-bound and processing is LLM-bound, and the gap is large: measured on
-- this stack, 145 chapters were fetched in 30 minutes while none finished translating.
-- Uncapped, a long serial is pulled down in hours onto a queue that then takes weeks to
-- drain — while the source site is hammered for content nothing can process yet.
--
-- Per scrape job rather than a process-wide setting because the right answer depends on
-- the book: a short novel can reasonably be fetched in one go, a 5000-chapter serial
-- should not be. NULL means "use the scraper's own default" (SCRAPE_MAX_QUEUE_DEPTH), so
-- existing jobs and callers that don't set it keep working unchanged.
BEGIN;

ALTER TABLE scrape_job ADD COLUMN max_queue_depth INT;

COMMENT ON COLUMN scrape_job.max_queue_depth IS
  'Pause fetching while jobs:pending is at/above this depth. NULL = service default; 0 = no limit.';

COMMIT;
