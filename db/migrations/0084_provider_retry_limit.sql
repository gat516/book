-- 0084_provider_retry_limit.sql — bound automatic hosted-provider retries (§0, §5.4).
-- Provider backpressure belongs to the revision, not a chapter failure attempt.
BEGIN;

ALTER TABLE graph_revision
  ADD COLUMN provider_wait_attempts INT NOT NULL DEFAULT 0 CHECK (provider_wait_attempts >= 0);
ALTER TABLE event_revision
  ADD COLUMN provider_wait_attempts INT NOT NULL DEFAULT 0 CHECK (provider_wait_attempts >= 0);

COMMIT;
