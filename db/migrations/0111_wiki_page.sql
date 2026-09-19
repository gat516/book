-- 0111_wiki_page.sql -- character wiki pages, one version per chapter.
--
-- A page is built chapter by chapter: when a chapter's facts name a character, that
-- character's page is rewritten from its previous version plus those facts, and the
-- result is stored as the page "as of" that chapter. A reader at chapter N sees each
-- page's newest version with chapter_index <= N, so the spoiler gate is just the chapter
-- a version was built at (§0: knowledge-time). Pages cost what a chapter mentions, not
-- what the whole book holds.
--
-- subject is the character's source term: one source term, one page (the exact-match
-- identity path). title is the spelling the page was written under. Append-only: a new
-- prompt adds a new set under its own prompt_version; rows are never edited.
BEGIN;

CREATE TABLE wiki_page (
  novel_id        UUID NOT NULL,
  subject         TEXT NOT NULL CHECK (btrim(subject) <> ''),
  chapter_index   INTEGER NOT NULL CHECK (chapter_index >= 1),
  prompt_version  TEXT NOT NULL,
  title           TEXT NOT NULL CHECK (btrim(title) <> ''),
  body            TEXT NOT NULL CHECK (btrim(body) <> ''),
  facts_used      INTEGER NOT NULL CHECK (facts_used >= 0),
  served_model    TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (novel_id, prompt_version, subject, chapter_index),
  FOREIGN KEY (novel_id, chapter_index) REFERENCES chapter (novel_id, chapter_index) ON DELETE CASCADE
);

CREATE FUNCTION guard_wiki_page_immutability() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'wiki_page rows are immutable';
END $$;
CREATE TRIGGER wiki_page_immutability BEFORE UPDATE ON wiki_page
  FOR EACH ROW EXECUTE FUNCTION guard_wiki_page_immutability();

ALTER TABLE wiki_page ENABLE ROW LEVEL SECURITY;
ALTER TABLE wiki_page FORCE ROW LEVEL SECURITY;
CREATE POLICY wiki_page_writer ON wiki_page FOR ALL TO ingest_writer USING (true) WITH CHECK (true);
GRANT SELECT, INSERT, DELETE ON wiki_page TO ingest_writer;

-- The second lock under reader-api's explicit chapter predicate: a reader sees a page
-- version only for their novel and only up to their chapter.
CREATE POLICY gate_wiki_page ON wiki_page FOR SELECT TO rls_reader
  USING (novel_id = reader_novel() AND chapter_index <= reader_chapter());
GRANT SELECT ON wiki_page TO rls_reader;

COMMIT;
