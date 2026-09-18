-- FACTS stage (.claude/plans/facts-stage.md): per-chapter story facts replace RECORDS.
--
-- 1. A per-book facts model, next to translate_model/extract_model (0041). Existing
--    books get their extract model, so an upgrade changes no book's behaviour.
-- 2. chapter_fact: one row per fact, keyed by chapter and prompt version. Append-only
--    (§0): a new prompt adds a new set under its own version; nothing is rewritten.
--    Deliberately NOT readable by rls_reader: facts are pipeline-internal until the wiki
--    step defines a read path, which must gate on chapter_index (knowledge-time, §0).
BEGIN;

ALTER TABLE novel_provider_config ADD COLUMN facts_model TEXT;
UPDATE novel_provider_config SET facts_model = extract_model WHERE extract_model IS NOT NULL;

CREATE TABLE chapter_fact (
  novel_id         UUID NOT NULL,
  chapter_index    INTEGER NOT NULL CHECK (chapter_index >= 0),
  prompt_version   TEXT NOT NULL,
  ordinal          INTEGER NOT NULL CHECK (ordinal >= 0),
  text             TEXT NOT NULL CHECK (btrim(text) <> ''),
  source_hash      TEXT NOT NULL,
  requested_model  TEXT NOT NULL,
  served_provider  TEXT,
  served_model     TEXT,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (novel_id, chapter_index, prompt_version, ordinal),
  FOREIGN KEY (novel_id, chapter_index) REFERENCES chapter (novel_id, chapter_index) ON DELETE CASCADE
);

-- Facts are never edited in place. Deletion stays possible (novel deletion cascades).
CREATE FUNCTION guard_chapter_fact_immutability() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'chapter_fact rows are immutable';
END $$;
CREATE TRIGGER chapter_fact_immutability BEFORE UPDATE ON chapter_fact
  FOR EACH ROW EXECUTE FUNCTION guard_chapter_fact_immutability();

ALTER TABLE chapter_fact ENABLE ROW LEVEL SECURITY;
ALTER TABLE chapter_fact FORCE ROW LEVEL SECURITY;
CREATE POLICY chapter_fact_writer ON chapter_fact FOR ALL TO ingest_writer USING (true) WITH CHECK (true);
GRANT SELECT, INSERT, DELETE ON chapter_fact TO ingest_writer;

COMMIT;
