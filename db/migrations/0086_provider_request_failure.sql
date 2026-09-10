-- Safe request rejection diagnostics (§0, §5.4); provider prose stays private.
BEGIN;
ALTER TABLE chapter_knowledge_run DROP CONSTRAINT chapter_knowledge_run_blocked_category_check;
ALTER TABLE chapter_knowledge_run ADD CONSTRAINT chapter_knowledge_run_blocked_category_check
  CHECK (blocked_category IS NULL OR blocked_category IN (
    'model_changed', 'input_changed', 'prompt_too_large',
    'serving_identity_changed', 'fenced', 'timeout', 'model_unreachable',
    'model_server_error', 'output_truncated', 'credential_missing',
    'credential_rejected', 'rate_limited', 'quota_exhausted',
    'model_not_available', 'unknown', 'revision_not_rebuildable',
    'review_rejected', 'not_found', 'cancelled', 'abandoned',
    'model_not_installed', 'provider_retry_exhausted',
    'provider_bad_request', 'provider_invalid_json'
  ));
COMMIT;
