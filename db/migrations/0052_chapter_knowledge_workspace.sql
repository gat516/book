-- 0052_chapter_knowledge_workspace.sql — chapter-scoped knowledge review and audit.
--
-- Facts stay append-only (§0.2). Only fact.value_en, a display-only gloss introduced by
-- 0051, may be overwritten; every such overwrite is still recorded below. Re-extraction
-- is a preview until an operator explicitly applies selected items.
BEGIN;

CREATE TABLE chapter_knowledge_run (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  novel_id UUID NOT NULL REFERENCES novel(id) ON DELETE CASCADE,
  chapter_index INT NOT NULL,
  revision_id UUID NOT NULL REFERENCES graph_revision(id),
  mode TEXT NOT NULL CHECK (mode IN ('ordinary','reextract')),
  state TEXT NOT NULL DEFAULT 'pending'
    CHECK (state IN ('pending','processing','awaiting_review','applying','published','rejected','failed')),
  input_hash TEXT NOT NULL,
  display_hash TEXT NOT NULL,
  model_identity TEXT NOT NULL,
  graph_generation BIGINT NOT NULL,
  graph_version BIGINT NOT NULL,
  preview JSONB,
  requested_by TEXT,
  error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  FOREIGN KEY (novel_id,chapter_index) REFERENCES chapter(novel_id,chapter_index) ON DELETE CASCADE,
  UNIQUE (id,novel_id,chapter_index)
);
CREATE UNIQUE INDEX chapter_knowledge_one_live_reextract
  ON chapter_knowledge_run(novel_id,chapter_index)
  WHERE mode='reextract' AND state IN ('pending','processing','awaiting_review','applying');
CREATE UNIQUE INDEX chapter_knowledge_one_ordinary
  ON chapter_knowledge_run(revision_id,chapter_index,mode) WHERE mode='ordinary';

CREATE TABLE chapter_knowledge_activity (
  seq BIGSERIAL PRIMARY KEY,
  run_id UUID NOT NULL REFERENCES chapter_knowledge_run(id) ON DELETE CASCADE,
  novel_id UUID NOT NULL,
  chapter_index INT NOT NULL,
  item_kind TEXT NOT NULL CHECK (item_kind IN ('fact','term','run')),
  item_key TEXT NOT NULL,
  phase TEXT NOT NULL CHECK (phase IN ('detected','proposed','verified','published','rejected')),
  payload JSONB NOT NULL DEFAULT '{}',
  idempotency_key TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  FOREIGN KEY (run_id,novel_id,chapter_index)
    REFERENCES chapter_knowledge_run(id,novel_id,chapter_index) ON DELETE CASCADE,
  UNIQUE(run_id,idempotency_key)
);
CREATE INDEX chapter_knowledge_activity_poll ON chapter_knowledge_activity(run_id,seq);

CREATE TABLE fact_edit_audit (
  id BIGSERIAL PRIMARY KEY,
  novel_id UUID NOT NULL REFERENCES novel(id),
  revision_id UUID NOT NULL REFERENCES graph_revision(id),
  fact_id BIGINT NOT NULL REFERENCES fact(id),
  action TEXT NOT NULL CHECK (action IN ('display','correction','retraction')),
  actor TEXT NOT NULL,
  old_display TEXT,
  new_display TEXT,
  successor_fact_id BIGINT REFERENCES fact(id),
  note TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX fact_edit_audit_fact ON fact_edit_audit(novel_id,fact_id,created_at);

-- A completion now says which chapter/run produced it. Nullable columns preserve every
-- existing cache row; new KnowledgeEngine writes populate them, including cache reuse.
ALTER TABLE graph_completion ADD COLUMN chapter_index INT;
ALTER TABLE graph_completion ADD COLUMN stage TEXT;
ALTER TABLE graph_completion ADD COLUMN batch_id TEXT;
ALTER TABLE graph_completion ADD COLUMN run_id UUID REFERENCES chapter_knowledge_run(id);
CREATE TABLE graph_completion_run (
  revision_id UUID NOT NULL,
  cache_key TEXT NOT NULL,
  served_provider TEXT NOT NULL,
  served_model TEXT NOT NULL,
  run_id UUID NOT NULL REFERENCES chapter_knowledge_run(id) ON DELETE CASCADE,
  chapter_index INT NOT NULL,
  stage TEXT NOT NULL,
  batch_id TEXT NOT NULL,
  PRIMARY KEY(revision_id,cache_key,served_provider,served_model,run_id),
  FOREIGN KEY(revision_id,cache_key,served_provider,served_model)
    REFERENCES graph_completion(revision_id,cache_key,served_provider,served_model) ON DELETE CASCADE
);
GRANT SELECT,INSERT ON graph_completion_run TO ingest_writer;

ALTER TABLE repair_request DROP CONSTRAINT repair_request_action_check;
ALTER TABLE repair_request ADD CONSTRAINT repair_request_action_check
  CHECK (action IN ('prepare','review','activate','rollback','reextract','reextract_apply'));
ALTER TABLE repair_request DROP CONSTRAINT repair_request_chapter_scope;
ALTER TABLE repair_request ADD CONSTRAINT repair_request_chapter_scope
  CHECK ((action IN ('reextract','reextract_apply')) = (chapter_index IS NOT NULL));

ALTER TABLE chapter_knowledge_run ENABLE ROW LEVEL SECURITY;
ALTER TABLE chapter_knowledge_run FORCE ROW LEVEL SECURITY;
CREATE POLICY gate_chapter_knowledge_run ON chapter_knowledge_run
  USING (novel_id=reader_novel() AND chapter_index<=reader_chapter()
         AND revision_id=reader_graph_revision());
ALTER TABLE chapter_knowledge_activity ENABLE ROW LEVEL SECURITY;
ALTER TABLE chapter_knowledge_activity FORCE ROW LEVEL SECURITY;
CREATE POLICY gate_chapter_knowledge_activity ON chapter_knowledge_activity
  USING (novel_id=reader_novel() AND chapter_index<=reader_chapter()
         AND EXISTS (SELECT 1 FROM chapter_knowledge_run r
                      WHERE r.id=run_id AND r.revision_id=reader_graph_revision()));
ALTER TABLE fact_edit_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE fact_edit_audit FORCE ROW LEVEL SECURITY;
CREATE POLICY gate_fact_edit_audit ON fact_edit_audit
  USING (novel_id=reader_novel() AND revision_id=reader_graph_revision()
         AND fact_id IN (SELECT id FROM fact));

GRANT SELECT ON chapter_knowledge_run,chapter_knowledge_activity,fact_edit_audit TO rls_reader;
GRANT SELECT,INSERT,UPDATE ON chapter_knowledge_run,chapter_knowledge_activity,fact_edit_audit TO ingest_writer;
GRANT USAGE,SELECT ON SEQUENCE chapter_knowledge_activity_seq_seq,fact_edit_audit_id_seq TO ingest_writer;
GRANT UPDATE(value_en) ON fact TO ingest_writer;

COMMIT;
