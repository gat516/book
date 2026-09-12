-- 0092_drop_legacy_knowledge_remnants.sql -- finish what 0089 started.
--
-- Two leftovers survived the legacy retirement:
--
-- 1. `mention_span.revision_id`. 0089 released its foreign key but left the column NOT
--    NULL, so DISPLAY_SCAN could no longer insert a span at all: the value it used to
--    supply came from `graph_revision`, which 0089 dropped. Spans are derived data keyed
--    by (novel_id, chapter_index) now; there is no revision to belong to.
-- 2. The `chapter_knowledge_*` tables -- the held-knowledge review path. Its Go handlers
--    (chapter_knowledge.go, knowledge_review.go) and Python producers are gone; records
--    are published per run, and the run is the unit of review.
--
-- Terminology survives untouched: `novel_vocabulary*` is a human decision record, not
-- extraction output, and `glossary*` likewise.
BEGIN;

ALTER TABLE mention_span DROP COLUMN IF EXISTS revision_id;

-- The policies on the tables below gate through this view, so the policies go first and
-- the view with them; every table they protect is dropped in the same transaction.
DROP POLICY IF EXISTS gate_chapter_knowledge_evidence_0079 ON chapter_knowledge_evidence;
DROP POLICY IF EXISTS gate_chapter_knowledge_participant_0079 ON chapter_knowledge_participant;
DROP VIEW IF EXISTS chapter_knowledge_current_item;

DROP TABLE IF EXISTS chapter_knowledge_change;
DROP TABLE IF EXISTS chapter_knowledge_evidence;
DROP TABLE IF EXISTS chapter_knowledge_participant;
DROP TABLE IF EXISTS chapter_knowledge_subject_link;
DROP TABLE IF EXISTS chapter_knowledge_subject;
DROP TABLE IF EXISTS chapter_knowledge_item;
DROP TABLE IF EXISTS chapter_knowledge_generation;

COMMIT;
