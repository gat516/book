-- 0112_characters_and_tagged_facts.sql -- facts tied to character IDs, tagged for the wiki.
--
-- A wiki page is assembled when read from a character's facts, grouped by tag (intro,
-- relationship, ability, ...). Facts refer to characters by ID, not spelling: FACTS
-- stores a marker in place of each name it finds, and reader-api fills it with the name's
-- current spelling. Confirming or correcting a spelling then changes every fact and page
-- at once, with no rewriting (the "Aruisi" facts that outlived the "Ares" correction).
--
-- 1. character: one per person's source term, created the first time a chapter's facts
--    name them (the exact-match identity path). Merging two names into one character is
--    not modelled yet.
-- 2. chapter_fact gains category, kind (a relationship's kind) and subjects (the
--    characters it names, in order). Older rows keep them NULL/empty.
-- 3. The wiki is the read path 0109 was waiting for: rls_reader may read facts and
--    characters, gated on its novel and chapter (§0 knowledge-time). reader-api also
--    filters on the chapter explicitly; RLS is the second lock.
BEGIN;

CREATE TABLE character (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  novel_id            UUID NOT NULL REFERENCES novel (id) ON DELETE CASCADE,
  source_term         TEXT NOT NULL CHECK (btrim(source_term) <> ''),
  first_seen_chapter  INTEGER NOT NULL CHECK (first_seen_chapter >= 0),
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (novel_id, source_term)
);

ALTER TABLE character ENABLE ROW LEVEL SECURITY;
ALTER TABLE character FORCE ROW LEVEL SECURITY;
CREATE POLICY character_writer ON character FOR ALL TO ingest_writer USING (true) WITH CHECK (true);
GRANT SELECT, INSERT, DELETE ON character TO ingest_writer;
CREATE POLICY gate_character ON character FOR SELECT TO rls_reader
  USING (novel_id = reader_novel() AND first_seen_chapter <= reader_chapter());
GRANT SELECT ON character TO rls_reader;

ALTER TABLE chapter_fact
  ADD COLUMN category TEXT,
  ADD COLUMN kind TEXT,
  ADD COLUMN subjects UUID[] NOT NULL DEFAULT '{}';

CREATE INDEX chapter_fact_subjects ON chapter_fact USING gin (subjects);

CREATE POLICY gate_chapter_fact ON chapter_fact FOR SELECT TO rls_reader
  USING (novel_id = reader_novel() AND chapter_index <= reader_chapter());
GRANT SELECT ON chapter_fact TO rls_reader;

COMMIT;
