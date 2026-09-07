-- 0063_mention_display_name.sql -- retain the target-language spelling of a source
-- occurrence separately from the entity it resolves to.
--
-- surface remains immutable source evidence. surface_en is display metadata populated
-- only from a verified source-to-translation alignment (§0).
BEGIN;

ALTER TABLE source_mention ADD COLUMN surface_en TEXT;
COMMENT ON COLUMN source_mention.surface_en IS
  'Display-only target-language occurrence spelling; never used for entity resolution.';

COMMIT;
