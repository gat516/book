-- 0088_records_legacy_reset.sql -- discard disposable knowledge data.
-- Chapter source/translation objects, progress, provider settings, glossary values and
-- human terminology/name-review decisions remain. Workers must be stopped first.
BEGIN;

UPDATE glossary SET entity_id = NULL;
UPDATE novel SET active_graph_revision = NULL, active_event_revision = NULL;
UPDATE job SET revision_id = NULL WHERE stage NOT IN ('extract','resolve','state','graph','event');
DELETE FROM chunk;

-- Explicit dependency order: shared chapter/progress/storage tables are untouched.
DELETE FROM novel_assertion_evidence;
DELETE FROM fact_edit_audit;
DELETE FROM knowledge_review_audit;
DELETE FROM chapter_knowledge_activity;
DELETE FROM chapter_knowledge_run;
DELETE FROM completion_cache_run;
DELETE FROM completion_cache;
DELETE FROM graph_completion_run;
DELETE FROM graph_completion;
DELETE FROM graph_audit;
DELETE FROM graph_job;
DELETE FROM graph_evidence;
DELETE FROM event_audit;
DELETE FROM event_completion;
DELETE FROM event_job;
DELETE FROM chapter_event_argument;
DELETE FROM chapter_event;
DELETE FROM event_revision;
DELETE FROM fact;
DELETE FROM edge;
DELETE FROM event;
DELETE FROM mention_binding;
DELETE FROM display_mention;
DELETE FROM source_mention;
DELETE FROM mention_span;
DELETE FROM term_rendering_occurrence;
DELETE FROM glossary_binding;
DELETE FROM glossary_candidate_chapter;
DELETE FROM glossary_candidate;
DELETE FROM glossary_proposal_chapter;
DELETE FROM alias;
DELETE FROM entity;
DELETE FROM graph_revision;

-- Only obsolete graph/event work is removed; translation and scraper work stays queued.
DELETE FROM job WHERE stage IN ('extract','resolve','state','graph','event');
COMMIT;
