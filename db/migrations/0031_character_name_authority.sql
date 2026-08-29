-- Source-anchored character-name authority and recoverable translation versions.
-- Character spellings are terminology, not identity: exact source occurrences remain
-- distinct while one novel-wide decision controls how a Hanzi surface is rendered.
BEGIN;

ALTER TABLE glossary
  ADD COLUMN constraint_class TEXT NOT NULL DEFAULT 'semantic_term'
  CHECK (constraint_class IN ('semantic_term', 'character_name'));

-- Semantic terms retain the poisoning guard. Character names may legitimately be
-- homophones, so their target spelling is not an identity key and need not be unique.
DROP INDEX glossary_novel_target_key;
CREATE UNIQUE INDEX glossary_novel_target_key
  ON glossary (novel_id, target_term)
  WHERE NOT deleted AND constraint_class = 'semantic_term';

CREATE TABLE character_name_review (
  novel_id UUID NOT NULL REFERENCES novel(id) ON DELETE CASCADE,
  source_term TEXT NOT NULL,
  first_seen_chapter INT NOT NULL,
  source_hash TEXT NOT NULL,
  char_start INT NOT NULL CHECK (char_start >= 0),
  char_end INT NOT NULL CHECK (char_end > char_start),
  quote TEXT NOT NULL,
  candidates JSONB NOT NULL DEFAULT '[]',
  reason TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved')),
  selected_target TEXT,
  selection_source TEXT CHECK (selection_source IN ('deterministic', 'offered', 'override')),
  reviewed_by TEXT,
  reviewed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (novel_id, source_term),
  CHECK ((status = 'pending' AND selected_target IS NULL)
      OR (status = 'approved' AND selected_target IS NOT NULL))
);

CREATE TABLE character_name_occurrence (
  id UUID PRIMARY KEY,
  novel_id UUID NOT NULL REFERENCES novel(id) ON DELETE CASCADE,
  chapter_index INT NOT NULL,
  source_hash TEXT NOT NULL,
  source_term TEXT NOT NULL,
  char_start INT NOT NULL CHECK (char_start >= 0),
  char_end INT NOT NULL CHECK (char_end > char_start),
  quote TEXT NOT NULL,
  FOREIGN KEY (novel_id, source_term)
    REFERENCES character_name_review(novel_id, source_term) ON DELETE CASCADE,
  UNIQUE (novel_id, chapter_index, source_hash, char_start, char_end)
);
CREATE INDEX character_name_occurrence_chapter
  ON character_name_occurrence(novel_id, chapter_index);

CREATE TABLE chapter_translation_version (
  novel_id UUID NOT NULL,
  chapter_index INT NOT NULL,
  version INT NOT NULL,
  translated_uri TEXT NOT NULL,
  translated_by TEXT NOT NULL,
  glossary_version INT,
  translation_fingerprint TEXT,
  reason TEXT NOT NULL DEFAULT 'initial',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (novel_id, chapter_index, version),
  FOREIGN KEY (novel_id, chapter_index)
    REFERENCES chapter(novel_id, chapter_index) ON DELETE CASCADE
);

INSERT INTO chapter_translation_version
  (novel_id, chapter_index, version, translated_uri, translated_by,
   glossary_version, reason)
SELECT novel_id, chapter_index, 1, translated_uri, COALESCE(translated_by, 'unknown'),
       glossary_version, 'pre-name-authority'
FROM chapter
WHERE translated_uri IS NOT NULL
ON CONFLICT DO NOTHING;

GRANT SELECT ON character_name_review, character_name_occurrence,
  chapter_translation_version TO rls_reader;

COMMIT;
