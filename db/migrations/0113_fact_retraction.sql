-- 0113_fact_retraction.sql -- a reader can remove a bad fact without deleting it.
--
-- Facts are never mutated (§0), so "delete this fact" records a retraction instead: the
-- fact row stays, and wiki pages leave out any fact with a retraction. One retraction
-- applies for every reader, like a name correction. A re-run of the same prompt keeps
-- the same (chapter, prompt_version, ordinal), so the retraction stays attached; a new
-- prompt version writes new facts, which start unretracted.
BEGIN;

CREATE TABLE fact_retraction (
  novel_id        UUID NOT NULL,
  chapter_index   INTEGER NOT NULL,
  prompt_version  TEXT NOT NULL,
  ordinal         INTEGER NOT NULL,
  retracted_by    TEXT NOT NULL CHECK (btrim(retracted_by) <> ''),
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (novel_id, chapter_index, prompt_version, ordinal),
  FOREIGN KEY (novel_id, chapter_index, prompt_version, ordinal)
    REFERENCES chapter_fact (novel_id, chapter_index, prompt_version, ordinal) ON DELETE CASCADE
);

ALTER TABLE fact_retraction ENABLE ROW LEVEL SECURITY;
ALTER TABLE fact_retraction FORCE ROW LEVEL SECURITY;
CREATE POLICY fact_retraction_writer ON fact_retraction FOR ALL TO ingest_writer USING (true) WITH CHECK (true);
GRANT SELECT, INSERT, DELETE ON fact_retraction TO ingest_writer;
CREATE POLICY gate_fact_retraction ON fact_retraction FOR SELECT TO rls_reader
  USING (novel_id = reader_novel() AND chapter_index <= reader_chapter());
GRANT SELECT ON fact_retraction TO rls_reader;

COMMIT;
