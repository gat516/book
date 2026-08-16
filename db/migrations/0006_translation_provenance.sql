-- Phase 1.7: translation provenance and durable per-novel voice pin.
BEGIN;

ALTER TABLE chapter ADD COLUMN translated_by TEXT;
ALTER TABLE novel ADD COLUMN translation_provider TEXT;

-- Serialize the glossary audit chain per novel and make a fork fail loudly.
ALTER TABLE glossary_changelog ADD COLUMN seq INT;
UPDATE glossary_changelog AS c
SET seq = ranked.seq
FROM (
  SELECT id, ROW_NUMBER() OVER (PARTITION BY novel_id ORDER BY id)::INT AS seq
  FROM glossary_changelog
) AS ranked
WHERE c.id = ranked.id;
ALTER TABLE glossary_changelog ALTER COLUMN seq SET NOT NULL;
ALTER TABLE glossary_changelog ADD CONSTRAINT glossary_changelog_novel_seq UNIQUE (novel_id, seq);

COMMIT;
