-- 0082_chapter_knowledge_blocked_category.sql — safe provider state for chapter-scoped
-- extraction (§0, §5.4). The raw `error` column remains write-side diagnostics only and
-- is deliberately never selected by reader-api.
BEGIN;

ALTER TABLE chapter_knowledge_run
  ADD COLUMN blocked_category TEXT,
  ADD COLUMN blocked_at TIMESTAMPTZ;

ALTER TABLE chapter_knowledge_run
  ADD CONSTRAINT chapter_knowledge_run_blocked_category_check
  CHECK (blocked_category IS NULL OR blocked_category IN (
    'model_changed', 'input_changed', 'prompt_too_large',
    'serving_identity_changed', 'fenced', 'timeout', 'model_unreachable',
    'model_server_error', 'output_truncated', 'credential_missing',
    'credential_rejected', 'rate_limited', 'quota_exhausted',
    'model_not_available', 'unknown', 'revision_not_rebuildable',
    'review_rejected', 'not_found', 'cancelled', 'abandoned',
    'model_not_installed'
  ));

-- The chapter list's progress connection reads only safe failure metadata. Keep the
-- raw error_type/detail outside its privilege set; those fields can contain provider
-- response material or source prose (0046).
GRANT SELECT (id, novel_id, chapter_index, error_code, occurred_at)
  ON chapter_failure TO reader_progress_writer;

COMMIT;
