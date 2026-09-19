-- 0115_wiki_subject_kinds.sql -- wiki pages for organizations, places and items too.
--
-- 0112's `character` table keyed a wiki page by a person's source term. The wiki now has
-- pages for organizations (sects, teams, any faction), places and items as well, so the
-- table becomes `subject` -- the name chapter_fact.subjects and the wiki API already use
-- -- with a `kind`. Identity is unchanged: one source term, one subject.
--
-- A person's kind comes from the names pass (term_role chinese_person/foreign_person).
-- Organizations, places and items share term_role semantic_term with techniques and
-- everything else, so the FACTS call classifies the names its facts mention (prompt
-- tagged-facts-v3). The first kind recorded for a source term stands; existing rows are
-- all people.
BEGIN;

ALTER TABLE character RENAME TO subject;
ALTER TABLE subject
  ADD COLUMN kind TEXT NOT NULL DEFAULT 'character'
    CHECK (kind IN ('character', 'organization', 'place', 'item'));
ALTER TABLE subject ALTER COLUMN kind DROP DEFAULT;

ALTER POLICY character_writer ON subject RENAME TO subject_writer;
ALTER POLICY gate_character ON subject RENAME TO gate_subject;

COMMIT;
