-- 0089_records_retire_legacy.sql -- remove the disposable graph/event machinery.
-- Only the records generation is reader knowledge after this point. The chapter, prose,
-- progress, provider and terminology tables are retained.
BEGIN;

-- Remove old write fences before removing their columns/tables. These names are explicit
-- inventory, rather than a broad CASCADE, so a newly added trigger cannot disappear by
-- accident.
DROP TRIGGER IF EXISTS initialize_graph_revision ON novel;
DROP TRIGGER IF EXISTS graph_write_fence ON entity;
DROP TRIGGER IF EXISTS graph_write_fence ON alias;
DROP TRIGGER IF EXISTS graph_write_fence ON fact;
DROP TRIGGER IF EXISTS graph_write_fence ON edge;
DROP TRIGGER IF EXISTS graph_write_fence ON event;
DROP TRIGGER IF EXISTS graph_write_fence ON mention_span;
DROP TRIGGER IF EXISTS validate_publication ON entity;
DROP TRIGGER IF EXISTS validate_publication ON alias;
DROP TRIGGER IF EXISTS validate_publication ON fact;
DROP TRIGGER IF EXISTS validate_publication ON edge;
DROP TRIGGER IF EXISTS validate_publication ON event;
DROP TRIGGER IF EXISTS graph_integrity ON fact;
DROP TRIGGER IF EXISTS graph_integrity ON edge;
DROP TRIGGER IF EXISTS graph_integrity ON event;
DROP TRIGGER IF EXISTS graph_integrity ON alias;
DROP TRIGGER IF EXISTS graph_integrity ON mention_span;
-- The same integrity trigger also guards the managed mention/glossary binding tables.
-- They are dropped below, but the function they depend on goes first, so name them here
-- rather than reaching for DROP ... CASCADE on a shared function.
DROP TRIGGER IF EXISTS graph_integrity ON mention_binding;
DROP TRIGGER IF EXISTS graph_integrity ON display_mention;
DROP TRIGGER IF EXISTS graph_integrity ON glossary_binding;
DROP TRIGGER IF EXISTS graph_integrity ON glossary_proposal_chapter;
DROP TRIGGER IF EXISTS graph_integrity ON source_mention;
DROP TRIGGER IF EXISTS graph_integrity ON graph_evidence;
DROP TRIGGER IF EXISTS edge_supersession_guard ON edge;
DROP TRIGGER IF EXISTS event_evidence_integrity ON event_evidence;
DROP TRIGGER IF EXISTS chapter_event_integrity ON chapter_event;
DROP TRIGGER IF EXISTS chapter_event_argument_integrity ON chapter_event_argument;


ALTER TABLE novel DROP CONSTRAINT IF EXISTS novel_active_graph_revision_fkey;
ALTER TABLE novel DROP CONSTRAINT IF EXISTS novel_active_event_revision_fkey;
ALTER TABLE novel DROP COLUMN IF EXISTS active_graph_revision;
ALTER TABLE novel DROP COLUMN IF EXISTS active_event_revision;

-- Remove old foreign keys/policies from the retained identity tables, then scope them to
-- records generations. Empty after 0088, so no data conversion is needed.
DROP POLICY IF EXISTS revision_entity ON entity;
DROP POLICY IF EXISTS revision_alias ON alias;
DROP POLICY IF EXISTS revision_fact ON fact;
DROP POLICY IF EXISTS revision_edge ON edge;
DROP POLICY IF EXISTS revision_event ON event;

-- entity, alias and mention_span survive, but their foreign keys point into tables that
-- do not. Release those first; the columns themselves come off after the table drops,
-- because entity's revision-scoped unique index is still referenced until then.
ALTER TABLE entity DROP CONSTRAINT IF EXISTS entity_revision_id_fkey;
ALTER TABLE alias DROP CONSTRAINT IF EXISTS alias_revision_id_fkey;
ALTER TABLE alias DROP CONSTRAINT IF EXISTS alias_evidence_revision;
ALTER TABLE alias DROP CONSTRAINT IF EXISTS alias_evidence_id_fkey;
ALTER TABLE alias DROP COLUMN IF EXISTS evidence_id;
ALTER TABLE mention_span DROP CONSTRAINT IF EXISTS mention_span_revision_id_fkey;
-- The job table outlives the graph; only its pointer into it goes, and it must go before
-- graph_revision is dropped.
ALTER TABLE job DROP CONSTRAINT IF EXISTS job_revision_id_fkey;
ALTER TABLE job DROP COLUMN IF EXISTS revision_id;

-- Child-first table retirement. These are all disposable knowledge/review/cache tables;
-- chapter, glossary, vocabulary, translation and scraper tables are absent from this list.
-- Child-first, in an order derived from the live foreign-key graph: every table is
-- dropped only after everything referencing it. An explicit inventory, not a CASCADE,
-- so a table added later cannot disappear silently.
DROP TABLE IF EXISTS novel_assertion_evidence;
DROP TABLE IF EXISTS fact_edit_audit;
DROP TABLE IF EXISTS knowledge_review_audit;
DROP TABLE IF EXISTS chapter_knowledge_activity;
DROP TABLE IF EXISTS graph_completion_run;
DROP TABLE IF EXISTS graph_completion;
DROP TABLE IF EXISTS completion_cache_run;
DROP TABLE IF EXISTS completion_cache;
DROP TABLE IF EXISTS chapter_knowledge_run;
DROP TABLE IF EXISTS graph_job;
DROP TABLE IF EXISTS graph_audit;
DROP TABLE IF EXISTS event_audit;
DROP TABLE IF EXISTS event_completion;
DROP TABLE IF EXISTS event_job;
DROP TABLE IF EXISTS chapter_event_argument;
DROP TABLE IF EXISTS chapter_event;
DROP TABLE IF EXISTS event_evidence;
DROP TABLE IF EXISTS event_revision;
DROP TABLE IF EXISTS fact;
DROP TABLE IF EXISTS edge;
DROP TABLE IF EXISTS event;
DROP TABLE IF EXISTS display_mention;
DROP TABLE IF EXISTS mention_binding;
DROP TABLE IF EXISTS source_mention;
DROP TABLE IF EXISTS glossary_binding;
DROP TABLE IF EXISTS glossary_proposal_chapter;
DROP TABLE IF EXISTS graph_evidence;
DROP TABLE IF EXISTS graph_revision;

-- Identity tables keep their rows' shape but lose the revision scaffolding. This waits
-- until the knowledge tables are gone: their foreign keys referenced entity's
-- revision-scoped unique index, so dropping it earlier would need a CASCADE.
-- alias_same_revision is backed by entity's revision-scoped unique index, so it goes first.
ALTER TABLE alias DROP CONSTRAINT IF EXISTS alias_same_revision;
ALTER TABLE entity DROP CONSTRAINT IF EXISTS entity_revision_unique;
ALTER TABLE entity DROP CONSTRAINT IF EXISTS entity_revision_id_fkey;
ALTER TABLE alias DROP CONSTRAINT IF EXISTS alias_revision_id_fkey;
ALTER TABLE entity DROP COLUMN IF EXISTS revision_id;
ALTER TABLE alias DROP COLUMN IF EXISTS revision_id;

-- Safe now: the fence is gone and 0088 emptied both tables, so every surviving row is
-- written by the records publisher, which always supplies a generation.
ALTER TABLE entity ALTER COLUMN record_generation_id SET NOT NULL;
ALTER TABLE alias ALTER COLUMN record_generation_id SET NOT NULL;

-- Translation, character naming and records enrichment are the surviving pipeline jobs.
ALTER TABLE job DROP CONSTRAINT IF EXISTS job_stage_check;
ALTER TABLE job ADD CONSTRAINT job_stage_check CHECK (stage IN ('translate','character_names','records'));


-- Functions come out AFTER their tables: policies and triggers on the dropped tables
-- reference these functions, and dropping a function out from under them would need a
-- CASCADE that could take an unrelated object with it. Vocabulary functions are deliberately retained because terminology state is
-- part of the translation contract.
DROP FUNCTION IF EXISTS initialize_graph_revision();
DROP FUNCTION IF EXISTS guard_graph_write();
DROP FUNCTION IF EXISTS validate_graph_record();
DROP FUNCTION IF EXISTS allow_placeholder_revision();
DROP FUNCTION IF EXISTS guard_mention_write();
DROP FUNCTION IF EXISTS validate_published_claim();
DROP FUNCTION IF EXISTS reader_graph_revision();
DROP FUNCTION IF EXISTS reader_event_revision();
DROP FUNCTION IF EXISTS reader_knowledge_status(INT);
DROP FUNCTION IF EXISTS reader_event_status(INT);
DROP FUNCTION IF EXISTS reader_chapter_graph_extraction(UUID, INT);
DROP FUNCTION IF EXISTS reader_graph_worker_report(UUID);
DROP FUNCTION IF EXISTS repair_subject_revision(UUID, TEXT);
DROP FUNCTION IF EXISTS repair_preview(UUID, TEXT);
DROP FUNCTION IF EXISTS reader_repair_status(UUID);
DROP FUNCTION IF EXISTS reader_repair_failures(UUID);
DROP FUNCTION IF EXISTS reader_repair_requests(UUID);
DROP FUNCTION IF EXISTS reader_repair_history(UUID);
DROP FUNCTION IF EXISTS reader_repair_rollback_targets(UUID);
DROP FUNCTION IF EXISTS repair_progress(UUID);
DROP FUNCTION IF EXISTS repair_extraction(UUID);
DROP FUNCTION IF EXISTS repair_proposed_claims(UUID);
DROP FUNCTION IF EXISTS corroborated_fact_ids(UUID, UUID, INT);
DROP FUNCTION IF EXISTS reader_held_knowledge(UUID, INT);
DROP FUNCTION IF EXISTS reader_held_knowledge_bulk_eligible(UUID, INT);


-- Generation-scoped identity gates replace the removed revision policies. Entity and alias
-- rows are visible only through the active generation and the reader's chapter cap.
CREATE POLICY record_entity_gate ON entity AS RESTRICTIVE USING (
  novel_id=reader_novel() AND record_generation_id=(SELECT active_record_generation FROM novel WHERE id=reader_novel())
  AND first_seen_chapter<=reader_chapter()
);
CREATE POLICY record_alias_gate ON alias AS RESTRICTIVE USING (
  record_generation_id=(SELECT active_record_generation FROM novel WHERE id=reader_novel())
  AND first_seen_chapter<=reader_chapter() AND entity_id IN (SELECT id FROM entity)
);

-- Every records child is gated through its published parent run. A failed run is visible
-- only to the status/inspector SECURITY DEFINER function; source rows never escape it.
DO $$ DECLARE t TEXT; BEGIN
  FOREACH t IN ARRAY ARRAY['record_generation','record_run','record_passage','record_row','record_value','record_reference','record_participant','record_evidence','record_drop','record_rendering','record_mention_binding'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
  END LOOP;
END $$;
DROP POLICY IF EXISTS record_generation_gate ON record_generation;
DROP POLICY IF EXISTS record_run_gate ON record_run;
DROP POLICY IF EXISTS record_passage_gate ON record_passage;
DROP POLICY IF EXISTS record_row_gate ON record_row;
DROP POLICY IF EXISTS record_child_gate ON record_value;
DROP POLICY IF EXISTS record_ref_gate ON record_reference;
DROP POLICY IF EXISTS record_participant_gate ON record_participant;
DROP POLICY IF EXISTS record_evidence_gate ON record_evidence;
DROP POLICY IF EXISTS record_drop_gate ON record_drop;
DROP POLICY IF EXISTS record_rendering_gate ON record_rendering;
DROP POLICY IF EXISTS record_binding_gate ON record_mention_binding;
CREATE POLICY record_generation_gate ON record_generation USING (
  novel_id=reader_novel() AND id=(SELECT active_record_generation FROM novel WHERE id=reader_novel()) AND state='active'
);
CREATE POLICY record_run_gate ON record_run USING (
  novel_id=reader_novel() AND generation_id=(SELECT active_record_generation FROM novel WHERE id=reader_novel())
  AND status='published' AND chapter_index<=reader_chapter()
);
CREATE POLICY record_passage_gate ON record_passage USING (
  novel_id=reader_novel() AND chapter_index<=reader_chapter()
  AND EXISTS (SELECT 1 FROM record_run r WHERE r.id=run_id AND r.status='published')
);
CREATE POLICY record_row_gate ON record_row USING (
  novel_id=reader_novel() AND source_chapter<=reader_chapter()
  AND EXISTS (SELECT 1 FROM record_run r WHERE r.id=run_id AND r.status='published')
);
CREATE POLICY record_child_gate ON record_value USING (EXISTS (SELECT 1 FROM record_row r WHERE r.id=row_id));
CREATE POLICY record_ref_gate ON record_reference USING (
  novel_id=reader_novel() AND EXISTS (SELECT 1 FROM record_run r WHERE r.id=run_id AND r.status='published' AND r.chapter_index<=reader_chapter())
);
CREATE POLICY record_participant_gate ON record_participant USING (EXISTS (SELECT 1 FROM record_row r WHERE r.id=row_id));
CREATE POLICY record_evidence_gate ON record_evidence USING (
  EXISTS (SELECT 1 FROM record_row r JOIN record_run x ON x.id=r.run_id
    WHERE r.id=row_id AND r.run_id=record_evidence.run_id AND x.status='published' AND r.source_chapter<=reader_chapter())
);
CREATE POLICY record_drop_gate ON record_drop USING (
  novel_id=reader_novel() AND EXISTS (SELECT 1 FROM record_run r WHERE r.id=run_id AND r.status='published' AND r.chapter_index<=reader_chapter())
);
CREATE POLICY record_rendering_gate ON record_rendering USING (EXISTS (SELECT 1 FROM record_row r WHERE r.id=row_id));
CREATE POLICY record_binding_gate ON record_mention_binding USING (
  novel_id=reader_novel() AND source_chapter<=reader_chapter()
  AND EXISTS (SELECT 1 FROM record_run r WHERE r.id=run_id AND r.status='published')
);

GRANT SELECT ON record_generation,record_run,record_passage,record_row,record_value,record_reference,record_participant,record_evidence,record_drop,record_rendering,record_mention_binding TO rls_reader;
GRANT SELECT,INSERT,UPDATE,DELETE ON record_generation,record_run,record_passage,record_row,record_value,record_reference,record_participant,record_evidence,record_drop,record_rendering,record_mention_binding TO ingest_writer;

COMMIT;
