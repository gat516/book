-- 0104_fact_first_pipeline.sql -- fact-first extraction persistence.
--
-- The fact-first experiment has a different semantic contract from typed records:
-- candidates, decisions, assertions, and normalized native outputs are all retained
-- for replay and audit. Local IDs (p001, c1, c1.a1, e1) are deliberately text values;
-- they are scoped by a run and are mapped to durable identity only by the who's-who
-- pass. Reader policies use source_chapter (knowledge time), per spec §0.
BEGIN;

CREATE TABLE fact_first_run (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  novel_id UUID NOT NULL,
  generation_id UUID NOT NULL,
  chapter_index INT NOT NULL CHECK (chapter_index >= 0),
  source_hash TEXT NOT NULL,
  request_identity TEXT NOT NULL,
  baseline_commit TEXT NOT NULL,
  prompt_hashes JSONB NOT NULL DEFAULT '{}'::jsonb,
  prompt_variants JSONB NOT NULL DEFAULT '{}'::jsonb,
  provider TEXT,
  requested_model TEXT,
  served_model TEXT,
  budgets JSONB NOT NULL DEFAULT '{}'::jsonb,
  status TEXT NOT NULL DEFAULT 'processing'
    CHECK (status IN ('processing','published','failed','discarded')),
  diagnostics JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  published_at TIMESTAMPTZ,
  UNIQUE (novel_id, generation_id, chapter_index),
  FOREIGN KEY (generation_id, novel_id)
    REFERENCES record_generation(id, novel_id) ON DELETE CASCADE,
  FOREIGN KEY (novel_id, chapter_index)
    REFERENCES chapter(novel_id, chapter_index) ON DELETE CASCADE
);

CREATE TABLE fact_first_passage (
  run_id UUID NOT NULL REFERENCES fact_first_run(id) ON DELETE CASCADE,
  passage_id TEXT NOT NULL,
  text TEXT NOT NULL,
  char_start INT NOT NULL CHECK (char_start >= 0),
  char_end INT NOT NULL CHECK (char_end >= char_start),
  ordinal INT NOT NULL CHECK (ordinal >= 0),
  PRIMARY KEY (run_id, passage_id),
  UNIQUE (run_id, ordinal)
);

CREATE TABLE fact_first_candidate (
  run_id UUID NOT NULL REFERENCES fact_first_run(id) ON DELETE CASCADE,
  candidate_id TEXT NOT NULL,
  source_text TEXT NOT NULL,
  passage_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
  char_start INT CHECK (char_start >= 0),
  char_end INT CHECK (char_end >= char_start),
  ordinal INT NOT NULL CHECK (ordinal >= 0),
  status TEXT NOT NULL DEFAULT 'accepted' CHECK (status IN ('accepted','rejected')),
  rejection_reason TEXT,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  PRIMARY KEY (run_id, candidate_id),
  UNIQUE (run_id, ordinal)
);

CREATE TABLE fact_first_selection (
  run_id UUID NOT NULL,
  candidate_id TEXT NOT NULL,
  decision TEXT NOT NULL CHECK (decision IN ('keep','omit','consolidate')),
  kind TEXT,
  consolidate_into TEXT,
  reason TEXT,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  PRIMARY KEY (run_id, candidate_id),
  FOREIGN KEY (run_id, candidate_id)
    REFERENCES fact_first_candidate(run_id, candidate_id) ON DELETE CASCADE,
  CHECK ((decision = 'consolidate') = (consolidate_into IS NOT NULL))
);

CREATE TABLE fact_first_assertion (
  run_id UUID NOT NULL,
  assertion_id TEXT NOT NULL,
  candidate_id TEXT NOT NULL,
  statement TEXT NOT NULL,
  outcome TEXT NOT NULL CHECK (outcome IN ('represented','unresolved','omitted','rejected')),
  status TEXT NOT NULL DEFAULT 'represented'
    CHECK (status IN ('represented','lost','unresolved','omitted')),
  reason TEXT,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  PRIMARY KEY (run_id, assertion_id),
  FOREIGN KEY (run_id, candidate_id)
    REFERENCES fact_first_candidate(run_id, candidate_id) ON DELETE CASCADE
);

CREATE TABLE fact_first_entity_proposal (
  run_id UUID NOT NULL,
  proposal_id TEXT NOT NULL,
  source_name TEXT NOT NULL,
  aliases JSONB NOT NULL DEFAULT '[]'::jsonb,
  kind TEXT,
  english_name TEXT,
  passage_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
  persistent_entity_id UUID,
  resolution_status TEXT NOT NULL DEFAULT 'unresolved'
    CHECK (resolution_status IN ('unresolved','resolved','rejected')),
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  FOREIGN KEY (persistent_entity_id) REFERENCES entity(id),
  PRIMARY KEY (run_id, proposal_id),
  FOREIGN KEY (run_id) REFERENCES fact_first_run(id) ON DELETE CASCADE
);

CREATE TABLE fact_first_reference (
  run_id UUID NOT NULL,
  reference_id TEXT NOT NULL,
  assertion_id TEXT,
  surface TEXT NOT NULL,
  refers_to TEXT,
  candidate_proposal_id TEXT,
  reason TEXT,
  claim_id TEXT,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  persistent_entity_id UUID,
  PRIMARY KEY (run_id, reference_id),
  FOREIGN KEY (run_id) REFERENCES fact_first_run(id) ON DELETE CASCADE,
  FOREIGN KEY (persistent_entity_id) REFERENCES entity(id),
  FOREIGN KEY (run_id, assertion_id)
    REFERENCES fact_first_assertion(run_id, assertion_id) ON DELETE CASCADE,
  FOREIGN KEY (run_id, candidate_proposal_id)
    REFERENCES fact_first_entity_proposal(run_id, proposal_id) ON DELETE CASCADE
);

-- These three tables retain the normalized assertion fields separately. JSON arguments
-- are intentional: event literals, conditions, and temporal qualifiers must survive
-- normalization without being forced into an entity ID.
CREATE TABLE fact_first_fact (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(), run_id UUID NOT NULL,
  local_id TEXT,
  assertion_id TEXT NOT NULL, subject_ref TEXT NOT NULL, attribute TEXT NOT NULL,
  value TEXT NOT NULL, source_value TEXT, polarity TEXT, attribution TEXT,
  condition TEXT, temporal TEXT, source_chapter INT NOT NULL, valid_from_chapter INT,
  evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  FOREIGN KEY (run_id, assertion_id) REFERENCES fact_first_assertion(run_id, assertion_id) ON DELETE CASCADE
);
CREATE TABLE fact_first_relation (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(), run_id UUID NOT NULL,
  local_id TEXT,
  assertion_id TEXT NOT NULL, src_ref TEXT NOT NULL, dst_ref TEXT NOT NULL,
  relation TEXT NOT NULL, source_value TEXT, polarity TEXT, attribution TEXT,
  condition TEXT, temporal TEXT, source_chapter INT NOT NULL, valid_from_chapter INT,
  evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  FOREIGN KEY (run_id, assertion_id) REFERENCES fact_first_assertion(run_id, assertion_id) ON DELETE CASCADE
);
CREATE TABLE fact_first_event (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(), run_id UUID NOT NULL,
  local_id TEXT,
  assertion_id TEXT NOT NULL, action TEXT NOT NULL, arguments JSONB NOT NULL DEFAULT '[]'::jsonb,
  source_value TEXT, polarity TEXT, attribution TEXT, condition TEXT, temporal TEXT,
  source_chapter INT NOT NULL, valid_from_chapter INT, evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  FOREIGN KEY (run_id, assertion_id) REFERENCES fact_first_assertion(run_id, assertion_id) ON DELETE CASCADE
);

CREATE TABLE fact_first_rendering (
  run_id UUID NOT NULL, assertion_id TEXT NOT NULL, output_kind TEXT NOT NULL,
  output_id TEXT NOT NULL, target_value TEXT, provider TEXT, served_model TEXT,
  status TEXT NOT NULL CHECK (status IN ('pending','ready','failed')),
  error_detail TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (run_id, assertion_id, output_kind, output_id),
  FOREIGN KEY (run_id, assertion_id) REFERENCES fact_first_assertion(run_id, assertion_id) ON DELETE CASCADE
);

CREATE INDEX fact_first_visible_idx
  ON fact_first_run(novel_id, generation_id, chapter_index)
  WHERE status = 'published';

CREATE FUNCTION guard_fact_first_run_immutability() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.status = 'published' AND (TG_OP = 'DELETE' OR NEW IS DISTINCT FROM OLD)
  THEN RAISE EXCEPTION 'published fact-first run content is immutable'; END IF;
  RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END $$;
CREATE TRIGGER fact_first_run_immutability BEFORE UPDATE OR DELETE ON fact_first_run
  FOR EACH ROW EXECUTE FUNCTION guard_fact_first_run_immutability();

CREATE FUNCTION guard_fact_first_child_immutability() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE rid UUID;
BEGIN
  rid := CASE WHEN TG_OP = 'DELETE' THEN OLD.run_id ELSE NEW.run_id END;
  IF TG_OP = 'UPDATE' AND EXISTS (
    SELECT 1 FROM fact_first_run WHERE id=OLD.run_id AND status='published'
  ) THEN RAISE EXCEPTION 'published fact-first run children are immutable'; END IF;
  IF EXISTS (SELECT 1 FROM fact_first_run WHERE id=rid AND status='published')
     AND TG_OP IN ('INSERT','UPDATE','DELETE')
  THEN RAISE EXCEPTION 'published fact-first run children are immutable'; END IF;
  RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END $$;
DO $$ DECLARE t TEXT; BEGIN
  FOREACH t IN ARRAY ARRAY['fact_first_passage','fact_first_candidate','fact_first_selection',
    'fact_first_assertion','fact_first_entity_proposal','fact_first_reference','fact_first_fact',
    'fact_first_relation','fact_first_event','fact_first_rendering'] LOOP
    EXECUTE format('CREATE TRIGGER %I BEFORE INSERT OR UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION guard_fact_first_child_immutability()', t || '_immutability', t);
  END LOOP;
END $$;

ALTER TABLE fact_first_run ENABLE ROW LEVEL SECURITY;
ALTER TABLE fact_first_passage ENABLE ROW LEVEL SECURITY;
ALTER TABLE fact_first_candidate ENABLE ROW LEVEL SECURITY;
ALTER TABLE fact_first_selection ENABLE ROW LEVEL SECURITY;
ALTER TABLE fact_first_assertion ENABLE ROW LEVEL SECURITY;
ALTER TABLE fact_first_entity_proposal ENABLE ROW LEVEL SECURITY;
ALTER TABLE fact_first_reference ENABLE ROW LEVEL SECURITY;
ALTER TABLE fact_first_fact ENABLE ROW LEVEL SECURITY;
ALTER TABLE fact_first_relation ENABLE ROW LEVEL SECURITY;
ALTER TABLE fact_first_event ENABLE ROW LEVEL SECURITY;
ALTER TABLE fact_first_rendering ENABLE ROW LEVEL SECURITY;

CREATE POLICY fact_first_run_reader ON fact_first_run USING (
  novel_id = reader_novel()
  AND generation_id = (SELECT active_record_generation FROM novel WHERE id = reader_novel())
  AND chapter_index <= reader_chapter()
  AND status = 'published'
);
CREATE POLICY fact_first_run_writer ON fact_first_run FOR ALL TO ingest_writer USING (true) WITH CHECK (true);

-- Child reads inherit the run's chapter and generation gate. This is deliberately a
-- WHERE policy on every table so a future API cannot accidentally bypass spoiler auth.
DO $$ DECLARE t TEXT; BEGIN
  FOREACH t IN ARRAY ARRAY['fact_first_passage','fact_first_candidate','fact_first_selection',
    'fact_first_assertion','fact_first_entity_proposal','fact_first_reference','fact_first_rendering'] LOOP
    EXECUTE format('CREATE POLICY %I_reader ON %I USING (EXISTS (SELECT 1 FROM fact_first_run r WHERE r.id=run_id AND r.status=''published'' AND r.novel_id=reader_novel() AND r.generation_id=(SELECT active_record_generation FROM novel WHERE id=reader_novel()) AND r.chapter_index<=reader_chapter()))', t, t);
    EXECUTE format('CREATE POLICY %I_writer ON %I FOR ALL TO ingest_writer USING (true) WITH CHECK (true)', t, t);
  END LOOP;
END $$;

CREATE POLICY fact_first_fact_reader ON fact_first_fact USING (
  source_chapter <= reader_chapter() AND EXISTS (SELECT 1 FROM fact_first_run r
    WHERE r.id=run_id AND r.status='published' AND r.novel_id=reader_novel()
      AND r.generation_id=(SELECT active_record_generation FROM novel WHERE id=reader_novel())
      AND r.chapter_index<=reader_chapter())
);
CREATE POLICY fact_first_relation_reader ON fact_first_relation USING (
  source_chapter <= reader_chapter() AND EXISTS (SELECT 1 FROM fact_first_run r
    WHERE r.id=run_id AND r.status='published' AND r.novel_id=reader_novel()
      AND r.generation_id=(SELECT active_record_generation FROM novel WHERE id=reader_novel())
      AND r.chapter_index<=reader_chapter())
);
CREATE POLICY fact_first_event_reader ON fact_first_event USING (
  source_chapter <= reader_chapter() AND EXISTS (SELECT 1 FROM fact_first_run r
    WHERE r.id=run_id AND r.status='published' AND r.novel_id=reader_novel()
      AND r.generation_id=(SELECT active_record_generation FROM novel WHERE id=reader_novel())
      AND r.chapter_index<=reader_chapter())
);
CREATE POLICY fact_first_fact_writer ON fact_first_fact FOR ALL TO ingest_writer USING (true) WITH CHECK (true);
CREATE POLICY fact_first_relation_writer ON fact_first_relation FOR ALL TO ingest_writer USING (true) WITH CHECK (true);
CREATE POLICY fact_first_event_writer ON fact_first_event FOR ALL TO ingest_writer USING (true) WITH CHECK (true);

ALTER TABLE fact_first_run FORCE ROW LEVEL SECURITY;
ALTER TABLE fact_first_passage FORCE ROW LEVEL SECURITY;
ALTER TABLE fact_first_candidate FORCE ROW LEVEL SECURITY;
ALTER TABLE fact_first_selection FORCE ROW LEVEL SECURITY;
ALTER TABLE fact_first_assertion FORCE ROW LEVEL SECURITY;
ALTER TABLE fact_first_entity_proposal FORCE ROW LEVEL SECURITY;
ALTER TABLE fact_first_reference FORCE ROW LEVEL SECURITY;
ALTER TABLE fact_first_fact FORCE ROW LEVEL SECURITY;
ALTER TABLE fact_first_relation FORCE ROW LEVEL SECURITY;
ALTER TABLE fact_first_event FORCE ROW LEVEL SECURITY;
ALTER TABLE fact_first_rendering FORCE ROW LEVEL SECURITY;

GRANT SELECT ON fact_first_run, fact_first_passage, fact_first_candidate, fact_first_selection,
  fact_first_assertion, fact_first_entity_proposal, fact_first_reference, fact_first_fact,
  fact_first_relation, fact_first_event, fact_first_rendering TO rls_reader;
GRANT SELECT, INSERT, UPDATE, DELETE ON fact_first_run, fact_first_passage, fact_first_candidate,
  fact_first_selection, fact_first_assertion, fact_first_entity_proposal, fact_first_reference,
  fact_first_fact, fact_first_relation, fact_first_event, fact_first_rendering TO ingest_writer;

COMMIT;
