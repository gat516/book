-- 0062_entity_display_name.sql -- target-language entity display without changing
-- source identity.
--
-- entity.canonical remains the source-anchored identity key. canonical_en is display
-- metadata, like fact.value_en (0051): it may be corrected without changing what entity
-- a source mention resolves to (§0).
BEGIN;

ALTER TABLE entity ADD COLUMN canonical_en TEXT;
COMMENT ON COLUMN entity.canonical_en IS
  'Display-only target-language entity name. Never used for resolution or evidence.';

COMMIT;
