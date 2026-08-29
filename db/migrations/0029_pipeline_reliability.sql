-- Scope durable work to the chapter that owns it, retain readable translations with
-- operational terminology warnings, and make graph retries schedulable.
BEGIN;

ALTER TABLE job DROP CONSTRAINT job_idempotency_key_key;
ALTER TABLE job ADD CONSTRAINT job_chapter_work_unique
  UNIQUE (novel_id, chapter_index, stage, idempotency_key);

ALTER TABLE chapter ADD COLUMN translation_warning_code TEXT;
ALTER TABLE chapter ADD COLUMN translation_warning_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE chapter ADD CONSTRAINT chapter_translation_warning_check CHECK (
  (translation_warning_code IS NULL AND translation_warning_count = 0)
  OR (translation_warning_code = 'locked_terms_missing' AND translation_warning_count > 0)
);

ALTER TABLE graph_job ADD COLUMN retry_at TIMESTAMPTZ;

CREATE INDEX entity_revision_kind_chapter
  ON entity(revision_id, kind, first_seen_chapter);

COMMIT;
