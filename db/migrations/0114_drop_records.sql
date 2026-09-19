-- 0114_drop_records.sql -- remove the RECORDS pipeline's storage.
--
-- FACTS replaced RECORDS (.claude/plans/facts-stage.md): one call per chapter writes
-- chapter_fact rows, and the wiki is built from them (0112). Nothing writes or reads the
-- records tables any more -- the pipeline stage, its publisher, the reader/askai read
-- paths, and the rebuild/review endpoints are all gone -- so this drops them, the same
-- way 0089 dropped the legacy graph once records had replaced it.
--
-- What goes:
-- 1. Every record_* and fact_first_* table, entity and alias, and the generation that
--    scoped them (record_generation, novel.active_record_generation and the trigger that
--    opened a generation for each new novel).
-- 2. The generation pins on chapter's retry columns. FACTS reads only its own chapter,
--    so a retry has no generation to be fenced to.
-- 3. mention_span.entity_id and glossary.entity_id. Only records' who's-who ever set
--    them; a span or glossary term is terminology, and identity lives on the wiki's
--    character table (0112).
-- 4. The functions only those tables used, and validate_event_revision_record, left over
--    from the legacy graph with no trigger attached since 0089.
--
-- Irreversible: extracted records for old books are deleted. chapter_fact, character,
-- wiki_page and every chapter/translation/glossary row are untouched.
BEGIN;

DROP TRIGGER novel_record_generation_init ON novel;
DROP FUNCTION initialize_record_generation();

DROP FUNCTION reader_fact_first_status(uuid, uuid, integer);
DROP FUNCTION reader_record_review_rows(uuid, uuid, integer);

-- Columns pointing INTO the records tables go first, so the tables can drop without
-- CASCADE (a CASCADE would hide any dependency this migration didn't anticipate).
ALTER TABLE mention_span DROP COLUMN entity_id;
ALTER TABLE glossary DROP COLUMN entity_id;
ALTER TABLE chapter
  DROP COLUMN enrichment_retry_generation_id,
  DROP COLUMN provider_retry_generation_id;
ALTER TABLE novel
  DROP CONSTRAINT novel_active_record_generation_fkey,
  DROP CONSTRAINT novel_active_record_generation_novel_fkey;

-- The fact_first_* policies read novel.active_record_generation, so the tables go
-- before the column does.
DROP TABLE
  fact_first_assertion, fact_first_candidate, fact_first_entity_proposal,
  fact_first_event, fact_first_fact, fact_first_passage, fact_first_reference,
  fact_first_relation, fact_first_rendering, fact_first_selection, fact_first_run,
  record_drop, record_evidence, record_mention_binding, record_participant,
  record_passage, record_reference, record_rendering, record_review_decision,
  record_value, record_row, record_run,
  alias, entity, record_generation;

ALTER TABLE novel DROP COLUMN active_record_generation;

DROP FUNCTION record_entity_not_rejected(uuid, uuid);
DROP FUNCTION record_row_not_rejected(uuid, uuid);
DROP FUNCTION guard_record_immutability();
DROP FUNCTION guard_fact_first_run_immutability();
DROP FUNCTION guard_fact_first_child_immutability();
DROP FUNCTION validate_event_revision_record();

COMMIT;
