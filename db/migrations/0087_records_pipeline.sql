-- 0087_records_pipeline.sql -- records storage seam.
-- New knowledge is append-only and scoped to an immutable extraction generation.
BEGIN;

CREATE TABLE record_generation (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  novel_id UUID NOT NULL REFERENCES novel(id) ON DELETE CASCADE,
  ontology JSONB NOT NULL,
  prompt_version TEXT NOT NULL,
  checks_version TEXT NOT NULL,
  extraction_model TEXT NOT NULL,
  rendering_model TEXT,
  source_lang TEXT NOT NULL,
  target_lang TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('pending','active','rebuilding','retired')),
  config_version BIGINT NOT NULL DEFAULT 1 CHECK (config_version > 0),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  retired_at TIMESTAMPTZ,
  UNIQUE (id, novel_id)
);

ALTER TABLE novel ADD COLUMN active_record_generation UUID;
ALTER TABLE novel ADD CONSTRAINT novel_active_record_generation_fkey
  FOREIGN KEY (active_record_generation) REFERENCES record_generation(id) ON DELETE SET NULL;
ALTER TABLE novel ADD CONSTRAINT novel_active_record_generation_novel_fkey
  FOREIGN KEY (active_record_generation, id) REFERENCES record_generation(id, novel_id);

INSERT INTO record_generation (novel_id, ontology, prompt_version, checks_version,
  extraction_model, rendering_model, source_lang, target_lang)
SELECT id, ontology, 'records-v1', 'records-v1', '', NULL, source_lang, target_lang FROM novel;
UPDATE novel n SET active_record_generation = g.id
FROM record_generation g WHERE g.novel_id = n.id;

CREATE OR REPLACE FUNCTION initialize_record_generation() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE gid UUID;
BEGIN
  INSERT INTO record_generation (novel_id, ontology, prompt_version, checks_version,
    extraction_model, rendering_model, source_lang, target_lang)
  VALUES (NEW.id, NEW.ontology, 'records-v1', 'records-v1', '', NULL,
    NEW.source_lang, NEW.target_lang) RETURNING id INTO gid;
  UPDATE novel SET active_record_generation = gid WHERE id = NEW.id;
  RETURN NEW;
END $$;
CREATE TRIGGER novel_record_generation_init AFTER INSERT ON novel
  FOR EACH ROW WHEN (NEW.active_record_generation IS NULL)
  EXECUTE FUNCTION initialize_record_generation();

-- Entity IDs are generation-local. Canonical source names and initial kinds are frozen;
-- later identity information is represented by chapter-indexed aliases and records.
-- Nullable here on purpose. The legacy write fence (guard_graph_write, 0023/0024) still
-- guards entity/alias at this point and rejects a backfill UPDATE, and 0088 empties both
-- tables anyway. 0089 sets NOT NULL once the fence is gone and the tables are empty.
ALTER TABLE entity ADD COLUMN record_generation_id UUID;
ALTER TABLE entity ADD CONSTRAINT entity_record_generation_fkey
  FOREIGN KEY (record_generation_id, novel_id) REFERENCES record_generation(id, novel_id) ON DELETE CASCADE;
ALTER TABLE entity ADD CONSTRAINT entity_record_scope_uq UNIQUE (id, novel_id, record_generation_id);
ALTER TABLE entity ADD CONSTRAINT entity_record_generation_uq UNIQUE (id, record_generation_id);
ALTER TABLE alias ADD COLUMN record_generation_id UUID;
ALTER TABLE alias ADD CONSTRAINT alias_record_generation_fkey
  FOREIGN KEY (entity_id, record_generation_id) REFERENCES entity(id, record_generation_id) ON DELETE CASCADE;
ALTER TABLE alias ADD CONSTRAINT alias_generation_scope_uq UNIQUE (entity_id, surface, lang, record_generation_id);

CREATE TABLE record_run (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  novel_id UUID NOT NULL,
  generation_id UUID NOT NULL,
  chapter_index INT NOT NULL CHECK (chapter_index >= 0),
  source_hash TEXT NOT NULL,
  display_hash TEXT,
  request_identity TEXT NOT NULL,
  extraction_model TEXT,
  served_provider TEXT,
  served_model TEXT,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','processing','published','failed')),
  diagnostics JSONB NOT NULL DEFAULT '{}'::jsonb,
  warning_count INT NOT NULL DEFAULT 0 CHECK (warning_count >= 0),
  attempts INT NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  claim_token UUID,
  claimed_at TIMESTAMPTZ,
  next_retry_at TIMESTAMPTZ,
  publication_version BIGINT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  published_at TIMESTAMPTZ,
  UNIQUE (id, novel_id, generation_id),
  UNIQUE (novel_id, generation_id, chapter_index),
  UNIQUE (id, chapter_index, source_hash),
  FOREIGN KEY (generation_id, novel_id) REFERENCES record_generation(id, novel_id) ON DELETE CASCADE,
  FOREIGN KEY (novel_id, chapter_index) REFERENCES chapter(novel_id, chapter_index) ON DELETE CASCADE
);
CREATE UNIQUE INDEX record_run_one_published
  ON record_run(novel_id, generation_id, chapter_index) WHERE status='published';
CREATE UNIQUE INDEX record_run_publication_version
  ON record_run(generation_id, publication_version) WHERE publication_version IS NOT NULL;
CREATE INDEX record_run_visible_idx ON record_run(novel_id, generation_id, chapter_index)
  WHERE status='published';

CREATE TABLE record_passage (
  novel_id UUID NOT NULL,
  generation_id UUID NOT NULL,
  run_id UUID NOT NULL,
  chapter_index INT NOT NULL CHECK (chapter_index >= 0),
  passage_id TEXT NOT NULL,
  text TEXT NOT NULL,
  char_start INT NOT NULL CHECK (char_start >= 0),
  char_end INT NOT NULL CHECK (char_end >= char_start),
  ordinal INT NOT NULL CHECK (ordinal >= 0),
  source_hash TEXT NOT NULL,
  PRIMARY KEY (run_id, passage_id),
  FOREIGN KEY (run_id, novel_id, generation_id) REFERENCES record_run(id, novel_id, generation_id) ON DELETE CASCADE,
  FOREIGN KEY (run_id, chapter_index, source_hash) REFERENCES record_run(id, chapter_index, source_hash),
  UNIQUE (run_id, ordinal)
);

CREATE TABLE record_row (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  novel_id UUID NOT NULL,
  generation_id UUID NOT NULL,
  run_id UUID NOT NULL,
  original_index INT NOT NULL CHECK (original_index >= 0),
  record_type TEXT NOT NULL,
  source_chapter INT NOT NULL CHECK (source_chapter >= 0),
  source_hash TEXT NOT NULL,
  valid_from_chapter INT,
  temporal_qualifier TEXT,
  UNIQUE (run_id, original_index),
  UNIQUE (id, novel_id, generation_id, run_id),
  UNIQUE (id, run_id),
  FOREIGN KEY (run_id, novel_id, generation_id) REFERENCES record_run(id, novel_id, generation_id) ON DELETE CASCADE,
  FOREIGN KEY (run_id, source_chapter, source_hash) REFERENCES record_run(id, chapter_index, source_hash)
);
CREATE INDEX record_row_chapter ON record_row(novel_id, generation_id, source_chapter, original_index);

CREATE TABLE record_value (
  row_id UUID NOT NULL REFERENCES record_row(id) ON DELETE CASCADE,
  field_name TEXT NOT NULL,
  source_value TEXT NOT NULL,
  PRIMARY KEY (row_id, field_name)
);

CREATE TABLE record_reference (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  novel_id UUID NOT NULL,
  generation_id UUID NOT NULL,
  run_id UUID NOT NULL,
  surface TEXT NOT NULL,
  proposed_kind TEXT,
  candidate_entity_id UUID,
  reason TEXT,
  UNIQUE (id, novel_id, generation_id, run_id),
  FOREIGN KEY (run_id, novel_id, generation_id) REFERENCES record_run(id, novel_id, generation_id) ON DELETE CASCADE,
  FOREIGN KEY (candidate_entity_id, novel_id, generation_id) REFERENCES entity(id, novel_id, record_generation_id)
);

CREATE TABLE record_participant (
  row_id UUID NOT NULL REFERENCES record_row(id) ON DELETE CASCADE,
  ordinal INT NOT NULL CHECK (ordinal >= 0),
  field_name TEXT NOT NULL,
  surface TEXT NOT NULL,
  entity_id UUID,
  reference_id UUID,
  PRIMARY KEY (row_id, ordinal),
  CHECK ((entity_id IS NULL) <> (reference_id IS NULL)),
  FOREIGN KEY (entity_id) REFERENCES entity(id),
  FOREIGN KEY (reference_id) REFERENCES record_reference(id) ON DELETE RESTRICT
);

CREATE TABLE record_evidence (
  row_id UUID NOT NULL,
  run_id UUID NOT NULL,
  passage_id TEXT NOT NULL,
  quote TEXT,
  PRIMARY KEY (row_id, passage_id),
  FOREIGN KEY (row_id, run_id) REFERENCES record_row(id, run_id) ON DELETE CASCADE,
  FOREIGN KEY (run_id, passage_id) REFERENCES record_passage(run_id, passage_id) DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE record_drop (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  novel_id UUID NOT NULL,
  generation_id UUID NOT NULL,
  run_id UUID NOT NULL,
  original_index INT,
  parsed_record JSONB,
  malformed_fragment TEXT,
  reasons JSONB NOT NULL,
  FOREIGN KEY (run_id, novel_id, generation_id) REFERENCES record_run(id, novel_id, generation_id) ON DELETE CASCADE
);

CREATE TABLE record_rendering (
  row_id UUID NOT NULL,
  field_name TEXT NOT NULL,
  target_value TEXT,
  provider TEXT,
  served_model TEXT,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','ready','failed')),
  error_detail TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (row_id, field_name),
  FOREIGN KEY (row_id, field_name) REFERENCES record_value(row_id, field_name) ON DELETE CASCADE
);

CREATE TABLE record_mention_binding (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  novel_id UUID NOT NULL,
  generation_id UUID NOT NULL,
  run_id UUID NOT NULL,
  mention_id UUID,
  row_id UUID,
  entity_id UUID,
  source_chapter INT NOT NULL CHECK (source_chapter >= 0),
  char_start INT NOT NULL CHECK (char_start >= 0),
  char_end INT NOT NULL CHECK (char_end >= char_start),
  FOREIGN KEY (run_id, novel_id, generation_id) REFERENCES record_run(id, novel_id, generation_id) ON DELETE CASCADE,
  FOREIGN KEY (row_id, run_id) REFERENCES record_row(id, run_id) ON DELETE CASCADE,
  FOREIGN KEY (entity_id, novel_id, generation_id) REFERENCES entity(id, novel_id, record_generation_id)
);

CREATE FUNCTION guard_record_immutability() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.status='published' AND (NEW.novel_id,NEW.generation_id,NEW.chapter_index,NEW.source_hash,
      NEW.display_hash,NEW.request_identity,NEW.extraction_model,NEW.served_provider,NEW.served_model)
      IS DISTINCT FROM (OLD.novel_id,OLD.generation_id,OLD.chapter_index,OLD.source_hash,
      OLD.display_hash,OLD.request_identity,OLD.extraction_model,OLD.served_provider,OLD.served_model)
  THEN RAISE EXCEPTION 'published record run content is immutable'; END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER record_run_immutability BEFORE UPDATE ON record_run
  FOR EACH ROW EXECUTE FUNCTION guard_record_immutability();

COMMIT;
