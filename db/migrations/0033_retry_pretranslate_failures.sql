-- Let a chapter that failed BEFORE translate be retried.
--
-- 0021 scoped enrichment retries to `translation_ready` chapters, on the reasonable
-- reading that a retry is about re-enriching a graph over prose already saved. But
-- CHARACTER_NAMES runs second of eight stages, so a transient model timeout there fails a
-- chapter that has no translation yet -- and such a chapter was then excluded from the
-- retry sweep entirely. status='error' with a NULL enrichment_retry_at is terminal: the
-- worker never looks at it again and no reader action requeues it. Every one of this
-- novel's chapters ended up in exactly that dead end.
--
-- The pre-translate failure is the one with nothing durable saved, so it is the most
-- important to retry, not the one to skip. The bounded enrichment_attempts < 3 cap in the
-- worker's sweep still stops a deterministically broken chapter.
BEGIN;

DROP INDEX IF EXISTS chapter_enrichment_retry;
CREATE INDEX chapter_enrichment_retry ON chapter(enrichment_retry_at)
WHERE status IN ('error', 'name_repair_error');

-- Recover chapters already stranded by the old predicate, the same way 0021 recovered
-- the failures that predated it. NULL retry_at is what made them unreachable; attempts
-- are left alone so an already-exhausted chapter stays exhausted.
UPDATE chapter SET enrichment_retry_at = now()
WHERE status IN ('error', 'name_repair_error')
  AND enrichment_retry_at IS NULL
  AND enrichment_attempts < 3;

COMMIT;
