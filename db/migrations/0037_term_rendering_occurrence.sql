-- Persist source-to-display terminology alignment for hover-card editing (§0, §5).
-- This is derived chapter data, not entity identity: source_term never implies entity_id.
BEGIN;

CREATE TABLE term_rendering_occurrence (
  novel_id UUID NOT NULL,
  chapter_index INT NOT NULL,
  char_start INT NOT NULL CHECK (char_start >= 0),
  char_end INT NOT NULL CHECK (char_end > char_start),
  source_term TEXT NOT NULL,
  display_term TEXT NOT NULL,
  method TEXT NOT NULL CHECK (method IN ('glossary','aligned')),
  PRIMARY KEY (novel_id, chapter_index, char_start, char_end),
  FOREIGN KEY (novel_id, chapter_index) REFERENCES chapter(novel_id, chapter_index) ON DELETE CASCADE,
  CHECK (char_length(display_term) = char_end - char_start)
);

ALTER TABLE term_rendering_occurrence ENABLE ROW LEVEL SECURITY;
ALTER TABLE term_rendering_occurrence FORCE ROW LEVEL SECURITY;
CREATE POLICY gate_term_rendering_occurrence ON term_rendering_occurrence
  USING (novel_id = reader_novel() AND chapter_index <= reader_chapter());
GRANT SELECT ON term_rendering_occurrence TO rls_reader;

COMMIT;
