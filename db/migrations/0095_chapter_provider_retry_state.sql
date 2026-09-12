-- 0095_chapter_provider_retry_state.sql — durable chapter provider backoff (§0, §5.4).
-- Provider admission waits are distinct from ordinary stage failures: the claim is
-- released immediately, while these fields tell the scheduler when it may re-enqueue
-- the chapter and bound consecutive provider rejections without holding Redis state.
BEGIN;

ALTER TABLE chapter
  ADD COLUMN provider_retry_attempts INT NOT NULL DEFAULT 0
    CHECK (provider_retry_attempts >= 0),
  ADD COLUMN provider_retry_at TIMESTAMPTZ,
  ADD COLUMN provider_retry_category TEXT,
  ADD COLUMN provider_retry_generation_id UUID,
  ADD COLUMN enrichment_retry_generation_id UUID;

ALTER TABLE chapter
  ADD CONSTRAINT chapter_provider_retry_generation_fkey
    FOREIGN KEY (provider_retry_generation_id, novel_id)
    REFERENCES record_generation(id, novel_id),
  ADD CONSTRAINT chapter_enrichment_retry_generation_fkey
    FOREIGN KEY (enrichment_retry_generation_id, novel_id)
    REFERENCES record_generation(id, novel_id);

CREATE INDEX chapter_provider_retry_due
  ON chapter(provider_retry_at)
  WHERE provider_retry_at IS NOT NULL;

-- records/status is served by the gated reader role. Expose only the bounded retry
-- state and allowlisted failure code needed to report terminal provider exhaustion;
-- provider response text and the rest of chapter metadata remain write/progress-side.
GRANT SELECT (novel_id, chapter_index, provider_retry_attempts, provider_retry_at,
              provider_retry_category, enrichment_attempts, enrichment_retry_at)
  ON chapter TO rls_reader;
GRANT SELECT (id, novel_id, chapter_index, error_code, occurred_at)
  ON chapter_failure TO rls_reader;

COMMIT;
