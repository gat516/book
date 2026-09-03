-- Independently reviewable chapter-action ledger (spec §0.1–§0.4).
--
-- Event quality must not be coupled to entity-link quality: an exact, supported action
-- remains useful when one of its named participants is not yet safe to bind.  Event
-- revisions therefore have their own activation pointer while optional entity links
-- retain the graph revision that justified them.
BEGIN;

CREATE TABLE event_revision (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  novel_id       UUID NOT NULL REFERENCES novel(id) ON DELETE CASCADE,
  state          TEXT NOT NULL DEFAULT 'staging'
                 CHECK (state IN ('staging', 'active', 'archived')),
  trusted        BOOLEAN NOT NULL DEFAULT false,
  generation     BIGINT NOT NULL DEFAULT 1,
  version        BIGINT NOT NULL DEFAULT 1,
  snapshot       JSONB NOT NULL DEFAULT '{}',
  event_schema   JSONB NOT NULL,
  model          JSONB NOT NULL DEFAULT '{}',
  prompt_version TEXT NOT NULL,
  evaluation     JSONB NOT NULL DEFAULT '{}',
  review         JSONB,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (novel_id, id)
);
CREATE UNIQUE INDEX event_one_active ON event_revision(novel_id) WHERE state = 'active';

ALTER TABLE novel ADD COLUMN active_event_revision UUID;
ALTER TABLE novel ADD CONSTRAINT novel_active_event_revision_fkey
  FOREIGN KEY (active_event_revision) REFERENCES event_revision(id) ON DELETE SET NULL;

CREATE TABLE event_audit (
  id          BIGSERIAL PRIMARY KEY,
  novel_id    UUID NOT NULL REFERENCES novel(id) ON DELETE CASCADE,
  revision_id UUID NOT NULL REFERENCES event_revision(id) ON DELETE CASCADE,
  action      TEXT NOT NULL,
  detail      JSONB NOT NULL DEFAULT '{}',
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE event_evidence (
  id            UUID PRIMARY KEY,
  revision_id   UUID NOT NULL REFERENCES event_revision(id) ON DELETE CASCADE,
  novel_id      UUID NOT NULL REFERENCES novel(id) ON DELETE CASCADE,
  chapter_index INT NOT NULL,
  source_hash   TEXT NOT NULL,
  char_start    INT NOT NULL CHECK (char_start >= 0),
  char_end      INT NOT NULL,
  quote         TEXT NOT NULL,
  CHECK (char_end > char_start AND char_length(quote) = char_end - char_start),
  UNIQUE (id, revision_id),
  UNIQUE (revision_id, chapter_index, source_hash, char_start, char_end)
);

CREATE TABLE chapter_event (
  id            UUID PRIMARY KEY,
  revision_id   UUID NOT NULL REFERENCES event_revision(id) ON DELETE CASCADE,
  novel_id      UUID NOT NULL REFERENCES novel(id) ON DELETE CASCADE,
  chapter_index INT NOT NULL,
  event_type    TEXT NOT NULL CHECK (btrim(event_type) <> ''),
  action        TEXT NOT NULL CHECK (btrim(action) <> ''),
  status        TEXT NOT NULL CHECK (status IN ('completed', 'attempted', 'prevented')),
  summary       TEXT NOT NULL CHECK (btrim(summary) <> ''),
  result        TEXT,
  evidence_id   UUID NOT NULL,
  claim_key     TEXT NOT NULL,
  UNIQUE (id, revision_id),
  UNIQUE (revision_id, claim_key),
  FOREIGN KEY (evidence_id, revision_id)
    REFERENCES event_evidence(id, revision_id) ON DELETE CASCADE
);
CREATE INDEX chapter_event_timeline ON chapter_event(revision_id, chapter_index, id);
CREATE INDEX chapter_event_search ON chapter_event USING gin
  (to_tsvector('simple', action || ' ' || summary || ' ' || coalesce(result, '')));

CREATE TABLE chapter_event_argument (
  revision_id          UUID NOT NULL REFERENCES event_revision(id) ON DELETE CASCADE,
  event_id             UUID NOT NULL,
  novel_id             UUID NOT NULL REFERENCES novel(id) ON DELETE CASCADE,
  chapter_index        INT NOT NULL,
  ordinal              INT NOT NULL CHECK (ordinal >= 0),
  role                 TEXT NOT NULL CHECK (btrim(role) <> ''),
  surface              TEXT NOT NULL CHECK (btrim(surface) <> ''),
  entity_id            UUID,
  linked_graph_revision UUID,
  PRIMARY KEY (event_id, ordinal),
  FOREIGN KEY (event_id, revision_id)
    REFERENCES chapter_event(id, revision_id) ON DELETE CASCADE,
  FOREIGN KEY (entity_id, linked_graph_revision)
    REFERENCES entity(id, revision_id),
  CHECK ((entity_id IS NULL) = (linked_graph_revision IS NULL))
);
CREATE INDEX chapter_event_argument_surface
  ON chapter_event_argument(revision_id, lower(surface));
CREATE INDEX chapter_event_argument_entity
  ON chapter_event_argument(entity_id) WHERE entity_id IS NOT NULL;

CREATE TABLE event_job (
  revision_id   UUID NOT NULL REFERENCES event_revision(id) ON DELETE CASCADE,
  chapter_index INT NOT NULL,
  state         TEXT NOT NULL DEFAULT 'pending'
                CHECK (state IN ('pending', 'processing', 'done', 'failed')),
  input_hash    TEXT NOT NULL,
  model_identity TEXT NOT NULL,
  attempts      INT NOT NULL DEFAULT 0,
  generation    BIGINT NOT NULL,
  error         TEXT,
  output        JSONB,
  retry_at      TIMESTAMPTZ,
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (revision_id, chapter_index)
);

CREATE TABLE event_completion (
  revision_id    UUID NOT NULL REFERENCES event_revision(id) ON DELETE CASCADE,
  cache_key      TEXT NOT NULL,
  served_provider TEXT NOT NULL CHECK (served_provider = 'ollama'),
  served_model   TEXT NOT NULL,
  response       JSONB NOT NULL,
  elapsed_seconds DOUBLE PRECISION NOT NULL,
  runtime_metrics JSONB NOT NULL DEFAULT '{}',
  PRIMARY KEY (revision_id, cache_key, served_provider, served_model)
);

CREATE FUNCTION reader_event_revision() RETURNS UUID
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT r.id
    FROM novel n
    JOIN event_revision r ON r.id = n.active_event_revision
   WHERE n.id = reader_novel() AND r.state = 'active' AND r.trusted
$$;

CREATE FUNCTION reader_event_status(ch INT)
RETURNS TABLE(revision_id UUID, version BIGINT, trusted BOOLEAN, status TEXT)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT r.id, r.version, r.trusted, coalesce(j.state, 'pending')
    FROM novel n
    JOIN event_revision r ON r.id = n.active_event_revision
    LEFT JOIN event_job j ON j.revision_id = r.id AND j.chapter_index = ch
   WHERE n.id = reader_novel() AND ch <= reader_chapter()
     AND r.state = 'active' AND r.trusted
$$;

CREATE FUNCTION validate_event_revision_record() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  r event_revision;
  ev event_evidence;
  e chapter_event;
  allowed_roles JSONB;
BEGIN
  SELECT * INTO r FROM event_revision WHERE id = NEW.revision_id FOR SHARE;
  IF NOT FOUND OR r.state = 'archived' THEN
    RAISE EXCEPTION 'event revision is fenced';
  END IF;
  IF NULLIF(current_setting('app.event_revision', true), '')::uuid IS DISTINCT FROM r.id
     OR coalesce(NULLIF(current_setting('app.event_generation', true), ''), '0')::bigint <> r.generation THEN
    RAISE EXCEPTION 'stale event generation';
  END IF;
  IF NEW.novel_id <> r.novel_id THEN
    RAISE EXCEPTION 'cross novel event record';
  END IF;

  IF TG_TABLE_NAME = 'event_evidence' THEN
    IF NEW.source_hash IS DISTINCT FROM (
      SELECT c->>'source_hash' FROM jsonb_array_elements(r.snapshot->'chapters') c
       WHERE (c->>'chapter')::int = NEW.chapter_index
    ) THEN
      RAISE EXCEPTION 'event evidence outside revision snapshot';
    END IF;
  ELSIF TG_TABLE_NAME = 'chapter_event' THEN
    SELECT * INTO ev FROM event_evidence
     WHERE id = NEW.evidence_id AND revision_id = r.id;
    IF NOT FOUND OR ev.chapter_index <> NEW.chapter_index THEN
      RAISE EXCEPTION 'event evidence belongs to another chapter';
    END IF;
    IF NOT EXISTS (
      SELECT 1 FROM jsonb_array_elements(r.event_schema->'types') t
       WHERE t->>'name' = NEW.event_type
    ) THEN
      RAISE EXCEPTION 'event type is outside revision schema';
    END IF;
  ELSE
    SELECT * INTO e FROM chapter_event
     WHERE id = NEW.event_id AND revision_id = r.id;
    IF NOT FOUND OR e.novel_id <> NEW.novel_id OR e.chapter_index <> NEW.chapter_index THEN
      RAISE EXCEPTION 'event argument belongs to another event';
    END IF;
    SELECT t->'roles' INTO allowed_roles
      FROM jsonb_array_elements(r.event_schema->'types') t
     WHERE t->>'name' = e.event_type;
    IF NOT (allowed_roles ? NEW.role) THEN
      RAISE EXCEPTION 'event argument role is outside event schema';
    END IF;
    SELECT * INTO ev FROM event_evidence WHERE id = e.evidence_id;
    IF position(NEW.surface IN ev.quote) = 0 THEN
      RAISE EXCEPTION 'event argument surface is absent from evidence';
    END IF;
    IF NEW.entity_id IS NOT NULL AND NOT EXISTS (
      SELECT 1 FROM entity linked
       WHERE linked.id = NEW.entity_id
         AND linked.revision_id = NEW.linked_graph_revision
         AND linked.novel_id = NEW.novel_id
         AND linked.first_seen_chapter <= NEW.chapter_index
    ) THEN
      RAISE EXCEPTION 'invalid event entity link';
    END IF;
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER event_evidence_integrity
  BEFORE INSERT OR UPDATE ON event_evidence
  FOR EACH ROW EXECUTE FUNCTION validate_event_revision_record();
CREATE TRIGGER chapter_event_integrity
  BEFORE INSERT OR UPDATE ON chapter_event
  FOR EACH ROW EXECUTE FUNCTION validate_event_revision_record();
CREATE TRIGGER chapter_event_argument_integrity
  BEFORE INSERT OR UPDATE ON chapter_event_argument
  FOR EACH ROW EXECUTE FUNCTION validate_event_revision_record();

ALTER TABLE event_evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE event_evidence FORCE ROW LEVEL SECURITY;
ALTER TABLE chapter_event ENABLE ROW LEVEL SECURITY;
ALTER TABLE chapter_event FORCE ROW LEVEL SECURITY;
ALTER TABLE chapter_event_argument ENABLE ROW LEVEL SECURITY;
ALTER TABLE chapter_event_argument FORCE ROW LEVEL SECURITY;

CREATE POLICY gate_event_evidence ON event_evidence USING (
  novel_id = reader_novel() AND revision_id = reader_event_revision()
  AND chapter_index <= reader_chapter()
);
CREATE POLICY gate_chapter_event ON chapter_event USING (
  novel_id = reader_novel() AND revision_id = reader_event_revision()
  AND chapter_index <= reader_chapter()
);
CREATE POLICY gate_chapter_event_argument ON chapter_event_argument USING (
  novel_id = reader_novel() AND revision_id = reader_event_revision()
  AND chapter_index <= reader_chapter()
);

GRANT SELECT ON event_evidence, chapter_event, chapter_event_argument TO rls_reader;
GRANT EXECUTE ON FUNCTION reader_event_revision(), reader_event_status(INT) TO rls_reader;

COMMIT;
