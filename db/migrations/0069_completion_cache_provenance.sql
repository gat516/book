-- 0069_completion_cache_provenance.sql — request identity and contract provenance (§5.4, §14.3).
--
-- 0068 is already applied. Keep its table shape immutable and add the metadata required to
-- distinguish requested from served identities and to revalidate a raw response under the
-- current stage contract. Existing rows receive empty metadata and are intentionally misses;
-- they cannot be safely rematerialized without the original request metadata.
BEGIN;

ALTER TABLE completion_cache
  ADD COLUMN requested_provider text NOT NULL DEFAULT '',
  ADD COLUMN requested_model text NOT NULL DEFAULT '',
  ADD COLUMN stage_prompt_version text NOT NULL DEFAULT '',
  ADD COLUMN prompt_digest text NOT NULL DEFAULT '',
  ADD COLUMN schema_digest text NOT NULL DEFAULT '';

ALTER TABLE completion_cache
  ALTER COLUMN requested_provider DROP DEFAULT,
  ALTER COLUMN requested_model DROP DEFAULT,
  ALTER COLUMN stage_prompt_version DROP DEFAULT,
  ALTER COLUMN prompt_digest DROP DEFAULT,
  ALTER COLUMN schema_digest DROP DEFAULT;

ALTER TABLE completion_cache_run
  ADD COLUMN requested_provider text NOT NULL DEFAULT '',
  ADD COLUMN requested_model text NOT NULL DEFAULT '',
  ADD COLUMN stage_prompt_version text NOT NULL DEFAULT '',
  ADD COLUMN prompt_digest text NOT NULL DEFAULT '',
  ADD COLUMN schema_digest text NOT NULL DEFAULT '';

ALTER TABLE completion_cache_run
  ALTER COLUMN requested_provider DROP DEFAULT,
  ALTER COLUMN requested_model DROP DEFAULT,
  ALTER COLUMN stage_prompt_version DROP DEFAULT,
  ALTER COLUMN prompt_digest DROP DEFAULT,
  ALTER COLUMN schema_digest DROP DEFAULT;

COMMIT;
