-- 0070_novel_vocabulary.sql — durable, novel-scoped attribute/relation vocabulary.
--
-- The frozen JSON ontology on novel is a seed, not an accumulation surface.  These
-- tables survive graph revisions, so corroboration remains meaningful after a rebuild.
-- Vocabulary rows do not add entity kinds: 0025's revision-ontology trigger remains the
-- authority for entity.kind.  Reader grants are deliberately deferred to the vocabulary
-- read model, which must redact future metadata as well as applying the row gate (§0.3).
BEGIN;

CREATE FUNCTION normalize_novel_vocabulary_name(raw TEXT)
RETURNS TEXT
LANGUAGE sql IMMUTABLE STRICT
AS $$
  SELECT lower(
           regexp_replace(
             regexp_replace(trim(raw), '[[:space:]/]+', '_', 'g'),
             '[^a-z0-9_]', '', 'g'
           )
         )
$$;

CREATE TABLE novel_vocabulary (
  novel_id             UUID NOT NULL REFERENCES novel(id) ON DELETE CASCADE,
  term_type            TEXT NOT NULL CHECK (term_type IN ('attribute', 'relation')),
  name                 TEXT NOT NULL
    CHECK (name ~ '^[a-z][a-z0-9_]{1,39}$'),
  kinds                TEXT[] NOT NULL DEFAULT '{}',
  dst_kinds            TEXT[] NOT NULL DEFAULT '{}',
  cardinality          TEXT NOT NULL DEFAULT 'accretive'
    CHECK (cardinality IN ('single', 'accretive')),
  polarity             SMALLINT NOT NULL DEFAULT 0
    CHECK (polarity BETWEEN -1 AND 1),
  status               TEXT NOT NULL DEFAULT 'candidate'
    CHECK (status IN ('candidate', 'admitted', 'retired', 'banned')),
  gloss                TEXT NOT NULL DEFAULT '',
  proposals            INT NOT NULL DEFAULT 0 CHECK (proposals >= 0),
  first_seen_chapter   INT NOT NULL DEFAULT 0 CHECK (first_seen_chapter >= 0),
  last_seen_chapter    INT NOT NULL DEFAULT 0 CHECK (last_seen_chapter >= 0),
  admitted_at_chapter  INT CHECK (admitted_at_chapter IS NULL OR admitted_at_chapter >= 0),
  embedding            VECTOR(768),
  version              BIGINT NOT NULL DEFAULT 1 CHECK (version > 0),
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (novel_id, term_type, name),
  CHECK (term_type <> 'relation' OR cardinality = 'accretive'),
  CHECK (term_type <> 'attribute' OR polarity = 0),
  CHECK (last_seen_chapter >= first_seen_chapter),
  CHECK (status <> 'admitted' OR admitted_at_chapter IS NOT NULL)
);

CREATE INDEX novel_vocabulary_embedding_hnsw
  ON novel_vocabulary USING hnsw (embedding vector_cosine_ops)
  WHERE embedding IS NOT NULL;
CREATE INDEX novel_vocabulary_visible
  ON novel_vocabulary (novel_id, term_type, first_seen_chapter, status);

-- This is the vocabulary-reuse ledger.  chapter_index is intentionally part of the
-- primary key: a retry of one chapter cannot vote twice (§0.7).
CREATE TABLE novel_vocabulary_chapter (
  novel_id       UUID NOT NULL,
  term_type      TEXT NOT NULL,
  name           TEXT NOT NULL,
  kind           TEXT NOT NULL,
  chapter_index  INT NOT NULL CHECK (chapter_index >= 0),
  revision_id    UUID,
  assertion_key  TEXT,
  evidence       JSONB NOT NULL DEFAULT '{}',
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (novel_id, term_type, name, kind, chapter_index),
  -- A chapter is one corroboration vote, even if a caller proposes the term for
  -- several entity kinds in that chapter (§0.7, C.4).
  UNIQUE (novel_id, term_type, name, chapter_index),
  FOREIGN KEY (novel_id, term_type, name)
    REFERENCES novel_vocabulary(novel_id, term_type, name) ON DELETE CASCADE
);
CREATE INDEX novel_vocabulary_chapter_count
  ON novel_vocabulary_chapter (novel_id, term_type, name, chapter_index);

-- A vocabulary alias is a chapter-visible, audited rename.  Keeping the chapter in the
-- key preserves the old name's history when a later correction points the same surface at
-- another canonical name.  surface is normalized so chain resolution is deterministic.
CREATE TABLE novel_vocabulary_alias (
  id                 BIGSERIAL PRIMARY KEY,
  novel_id           UUID NOT NULL,
  term_type          TEXT NOT NULL CHECK (term_type IN ('attribute', 'relation')),
  surface            TEXT NOT NULL
    CHECK (surface = normalize_novel_vocabulary_name(surface)),
  name               TEXT NOT NULL
    CHECK (name ~ '^[a-z][a-z0-9_]{1,39}$'),
  known_from_chapter INT NOT NULL CHECK (known_from_chapter >= 0),
  created_by         TEXT NOT NULL,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (novel_id, term_type, surface, known_from_chapter),
  -- ``name`` may be a canonical vocabulary name or another alias surface; the
  -- trigger below enforces same-novel/type target existence and permits chains.
  CONSTRAINT novel_vocabulary_alias_target_normalized_check
    CHECK (name = normalize_novel_vocabulary_name(name))
);
CREATE INDEX novel_vocabulary_alias_visible
  ON novel_vocabulary_alias (novel_id, term_type, surface, known_from_chapter DESC);

-- B.2.5's assertion evidence is separate from vocabulary recurrence.  Repeating an
-- attribute name does not corroborate the same assertion.  revision_id and chapter_index
-- are both in the uniqueness key so rebuild attribution and distinct chapters remain
-- explicit, while rerunning a chapter remains idempotent.
CREATE TABLE novel_assertion_evidence (
  novel_id             UUID NOT NULL,
  revision_id          UUID NOT NULL,
  entity_id            UUID NOT NULL,
  attribute            TEXT NOT NULL,
  assertion_signature  TEXT NOT NULL,
  chapter_index        INT NOT NULL CHECK (chapter_index >= 0),
  valid_from_chapter   INT NOT NULL CHECK (valid_from_chapter >= 0),
  evidence_id          UUID,
  source_hash          TEXT,
  value                TEXT,
  review_flag          TEXT,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (novel_id, revision_id, entity_id, attribute,
               assertion_signature, chapter_index),
  FOREIGN KEY (novel_id, revision_id)
    REFERENCES graph_revision(novel_id, id) ON DELETE CASCADE,
  FOREIGN KEY (entity_id, revision_id)
    REFERENCES entity(id, revision_id) ON DELETE CASCADE,
  FOREIGN KEY (evidence_id, revision_id)
    REFERENCES graph_evidence(id, revision_id) ON DELETE CASCADE
);
CREATE INDEX novel_assertion_evidence_lookup
  ON novel_assertion_evidence
     (novel_id, entity_id, attribute, assertion_signature, chapter_index);

-- Hash-chain audit for admission, ban, retirement, alias, and gloss changes.  The hash
-- payload is defined by the write path, as with glossary_changelog; the sequence and
-- previous-hash constraints make gaps/forks observable and reject duplicate positions.
CREATE TABLE novel_vocabulary_changelog (
  id                 BIGSERIAL PRIMARY KEY,
  novel_id           UUID NOT NULL REFERENCES novel(id) ON DELETE CASCADE,
  seq                INT NOT NULL CHECK (seq > 0),
  term_type          TEXT NOT NULL CHECK (term_type IN ('attribute', 'relation')),
  action             TEXT NOT NULL CHECK (action IN
                       ('admit', 'ban', 'retire', 'alias', 'gloss', 'cardinality', 'kinds')),
  name               TEXT NOT NULL,
  old_name           TEXT,
  new_name           TEXT,
  changed_at_chapter INT NOT NULL CHECK (changed_at_chapter >= 0),
  old_value          JSONB,
  new_value          JSONB,
  prev_hash          TEXT,
  row_hash           TEXT NOT NULL,
  created_by         TEXT NOT NULL,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (novel_id, seq)
);
CREATE INDEX novel_vocabulary_changelog_term
  ON novel_vocabulary_changelog (novel_id, term_type, name, seq);

ALTER TABLE edge
  ADD COLUMN sentiment SMALLINT CHECK (sentiment BETWEEN -1 AND 1),
  ADD COLUMN kind TEXT NOT NULL DEFAULT 'assertion'
    CHECK (kind IN ('assertion', 'retraction', 'correction')),
  ADD COLUMN supersedes BIGINT REFERENCES edge(id),
  ADD CONSTRAINT edge_no_self_supersession CHECK (supersedes IS NULL OR supersedes <> id);
CREATE INDEX edge_vocabulary_lookup
  ON edge (novel_id, rel_type, source_chapter);

-- Alias targets must already exist, and this trigger rejects a cycle in the chapter-visible
-- alias graph. A surface can have historical mappings, so each hop chooses the same
-- latest-known row as canonical_novel_vocabulary_name rather than branching on history.
CREATE FUNCTION guard_novel_vocabulary_alias_cycle()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
  current_name TEXT := NEW.name;
  next_name TEXT;
  visited TEXT[] := ARRAY[NEW.name]::TEXT[];
BEGIN
  IF NEW.surface = NEW.name THEN
    RAISE EXCEPTION 'vocabulary alias cannot target itself';
  END IF;
  IF NOT EXISTS (
       SELECT 1 FROM novel_vocabulary v
        WHERE v.novel_id=NEW.novel_id AND v.term_type=NEW.term_type AND v.name=NEW.name
     ) AND NOT EXISTS (
       SELECT 1 FROM novel_vocabulary_alias a
        WHERE a.novel_id=NEW.novel_id AND a.term_type=NEW.term_type AND a.surface=NEW.name
     ) THEN
    RAISE EXCEPTION 'vocabulary alias target %.% does not exist', NEW.term_type, NEW.name;
  END IF;
  LOOP
    SELECT a.name INTO next_name
      FROM novel_vocabulary_alias a
     WHERE a.novel_id = NEW.novel_id
       AND a.term_type = NEW.term_type
       AND a.surface = current_name
       AND a.known_from_chapter <= NEW.known_from_chapter
     ORDER BY a.known_from_chapter DESC, a.created_at DESC, a.id DESC
     LIMIT 1;
    EXIT WHEN NOT FOUND;
    IF next_name = NEW.surface OR next_name = ANY(visited) THEN
      RAISE EXCEPTION 'vocabulary alias cycle for %.%', NEW.term_type, NEW.surface;
    END IF;
    visited := visited || next_name;
    current_name := next_name;
  END LOOP;
  RETURN NEW;
END
$$;
CREATE TRIGGER novel_vocabulary_alias_cycle
  BEFORE INSERT OR UPDATE ON novel_vocabulary_alias
  FOR EACH ROW EXECUTE FUNCTION guard_novel_vocabulary_alias_cycle();

-- Resolve the canonical name at a reader's chapter.  Historical mappings choose the
-- latest known mapping; a malformed cycle fails closed instead of returning an arbitrary
-- label.  Application code can use this in read joins without mutating fact.attribute.
CREATE FUNCTION canonical_novel_vocabulary_name(
  p_novel_id UUID, p_term_type TEXT, p_name TEXT, p_chapter INT
)
RETURNS TEXT
LANGUAGE plpgsql STABLE
AS $$
DECLARE
  current_name TEXT := p_name;
  next_name TEXT;
  visited TEXT[] := ARRAY[p_name]::TEXT[];
BEGIN
  IF p_chapter < 0 THEN
    RETURN NULL;
  END IF;
  LOOP
    SELECT a.name INTO next_name
      FROM novel_vocabulary_alias a
     WHERE a.novel_id = p_novel_id
       AND a.term_type = p_term_type
       AND a.surface = current_name
       AND a.known_from_chapter <= p_chapter
     ORDER BY a.known_from_chapter DESC, a.created_at DESC, a.id DESC
     LIMIT 1;
    IF NOT FOUND THEN
      RETURN current_name;
    END IF;
    IF next_name = ANY(visited) THEN
      RETURN NULL;
    END IF;
    visited := visited || next_name;
    current_name := next_name;
  END LOOP;
END
$$;

-- Seed data is intentionally data-driven: ontology kinds come from each novel's JSON, and
-- the wiki defaults are only vocabulary names.  They never create or authorize entity kinds.
CREATE FUNCTION seed_novel_vocabulary(p_novel_id UUID)
RETURNS VOID
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public
AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM novel WHERE id = p_novel_id) THEN
    RETURN;
  END IF;

  -- The default set is deliberately broad over the novel's existing kinds.  The later
  -- proposal validator narrows a term to compatible source/destination kinds; this seed
  -- cannot invent a kind that is absent from novel.ontology.
  WITH defaults(name, cardinality, gloss) AS (VALUES
    ('status','single','durable lifecycle/status; not a transient scene condition'),
    ('appearance','accretive','lasting physical description; not what they are wearing right now'),
    ('personality','accretive','durable personality tendency; not a mood in this scene'),
    ('ability','accretive','durable capability; not an action performed once'),
    ('affiliation','single','durable membership or allegiance; not merely sharing a location'),
    ('role','single','durable role or office; not a one-scene action'),
    ('origin','single','where the entity comes from; not where it is standing in this scene'),
    ('location','single','where the entity is based or belongs; not where they are standing in this scene'),
    ('possession','accretive','durable ownership; not an object briefly held'),
    ('reputation','accretive','durable reputation held by others; not a single reaction'),
    ('danger','accretive','durable danger or threat status; not a momentary attack'),
    ('description','accretive','only when no more specific attribute applies')
  )
  INSERT INTO novel_vocabulary
    (novel_id,term_type,name,kinds,dst_kinds,cardinality,polarity,status,gloss,
     proposals,first_seen_chapter,last_seen_chapter,admitted_at_chapter)
  SELECT p_novel_id,'attribute',d.name,
         COALESCE((SELECT ARRAY_AGG(k ORDER BY k)
                     FROM jsonb_array_elements_text(n.ontology->'kinds') k), '{}'),
         '{}',d.cardinality,0,'admitted',d.gloss,0,0,0,0
    FROM defaults d CROSS JOIN novel n
    WHERE n.id=p_novel_id AND jsonb_array_length(COALESCE(n.ontology->'kinds','[]'::jsonb)) > 0
  ON CONFLICT(novel_id,term_type,name) DO UPDATE SET
    kinds = ARRAY(SELECT DISTINCT k FROM unnest(novel_vocabulary.kinds || EXCLUDED.kinds) k ORDER BY k),
    gloss = CASE WHEN novel_vocabulary.gloss='' THEN EXCLUDED.gloss ELSE novel_vocabulary.gloss END,
    status = CASE WHEN novel_vocabulary.status='candidate' THEN 'admitted' ELSE novel_vocabulary.status END,
    admitted_at_chapter = COALESCE(novel_vocabulary.admitted_at_chapter,0),
    updated_at = now();

  WITH defaults(name, polarity) AS (VALUES
    ('ally',1),('enemy',-1),('family',0),('mentor',1),('member_of',0),('located_in',0),
    ('romantic',1),('married_to',1),('rival',-1),('subordinate_of',0),
    ('distrusts',-1),('plots_against',-1)
  )
  INSERT INTO novel_vocabulary
    (novel_id,term_type,name,kinds,dst_kinds,cardinality,polarity,status,gloss,
     proposals,first_seen_chapter,last_seen_chapter,admitted_at_chapter)
  SELECT p_novel_id,'relation',d.name,
         COALESCE((SELECT ARRAY_AGG(k ORDER BY k)
                     FROM jsonb_array_elements_text(n.ontology->'kinds') k), '{}'),
         COALESCE((SELECT ARRAY_AGG(k ORDER BY k)
                     FROM jsonb_array_elements_text(n.ontology->'kinds') k), '{}'),
         'accretive',d.polarity,'admitted',
         'durable relationship; record only when it remains true after this scene',
         0,0,0,0
    FROM defaults d CROSS JOIN novel n
    WHERE n.id=p_novel_id AND jsonb_array_length(COALESCE(n.ontology->'kinds','[]'::jsonb)) > 0
  ON CONFLICT(novel_id,term_type,name) DO UPDATE SET
    kinds = ARRAY(SELECT DISTINCT k FROM unnest(novel_vocabulary.kinds || EXCLUDED.kinds) k ORDER BY k),
    dst_kinds = ARRAY(SELECT DISTINCT k FROM unnest(novel_vocabulary.dst_kinds || EXCLUDED.dst_kinds) k ORDER BY k),
    polarity = CASE WHEN novel_vocabulary.polarity=0 THEN EXCLUDED.polarity ELSE novel_vocabulary.polarity END,
    gloss = CASE WHEN novel_vocabulary.gloss='' THEN EXCLUDED.gloss ELSE novel_vocabulary.gloss END,
    status = CASE WHEN novel_vocabulary.status='candidate' THEN 'admitted' ELSE novel_vocabulary.status END,
    admitted_at_chapter = COALESCE(novel_vocabulary.admitted_at_chapter,0),
    updated_at = now();

  -- Existing ontology entries are admitted seeds too.  Object entries may restrict kinds;
  -- string entries inherit every configured kind.  Invalid/non-normalized names are left
  -- to the application proposal path rather than making migration fail on old data.
  WITH attrs AS (
    SELECT normalize_novel_vocabulary_name(
             CASE WHEN jsonb_typeof(a)='object' THEN a->>'name' ELSE a #>> '{}' END
           ) AS name,
           CASE WHEN jsonb_typeof(a)='object'
                     AND jsonb_typeof(a->'kinds')='array'
                THEN ARRAY(SELECT k FROM jsonb_array_elements_text(a->'kinds') k
                            WHERE n.ontology->'kinds' ? k)
                ELSE ARRAY(SELECT jsonb_array_elements_text(n.ontology->'kinds')) END AS kinds
      FROM novel n CROSS JOIN LATERAL jsonb_array_elements(COALESCE(n.ontology->'attributes','[]'::jsonb)) a
     WHERE n.id=p_novel_id
  )
  INSERT INTO novel_vocabulary
    (novel_id,term_type,name,kinds,dst_kinds,cardinality,polarity,status,gloss,
     proposals,first_seen_chapter,last_seen_chapter,admitted_at_chapter)
  SELECT p_novel_id,'attribute',a.name,a.kinds,'{}',
         CASE WHEN a.name IN ('status','affiliation','role','origin','location') THEN 'single' ELSE 'accretive' END,
         0,'admitted',format('durable %s; preserve the novel-specific evidence',a.name),0,0,0,0
    FROM attrs a
    WHERE a.name ~ '^[a-z][a-z0-9_]{1,39}$' AND cardinality(a.kinds) > 0
  ON CONFLICT(novel_id,term_type,name) DO UPDATE SET
    kinds = ARRAY(SELECT DISTINCT k FROM unnest(novel_vocabulary.kinds || EXCLUDED.kinds) k ORDER BY k),
    updated_at = now();

  WITH rels AS (
    SELECT normalize_novel_vocabulary_name(
             CASE WHEN jsonb_typeof(r)='object' THEN r->>'name' ELSE r #>> '{}' END
           ) AS name,
           CASE WHEN jsonb_typeof(r)='object'
                     AND jsonb_typeof(r->'src_kinds')='array'
                THEN ARRAY(SELECT k FROM jsonb_array_elements_text(r->'src_kinds') k
                            WHERE n.ontology->'kinds' ? k)
                WHEN jsonb_typeof(r)='object'
                     AND jsonb_typeof(r->'source_kinds')='array'
                THEN ARRAY(SELECT k FROM jsonb_array_elements_text(r->'source_kinds') k
                            WHERE n.ontology->'kinds' ? k)
                WHEN jsonb_typeof(r)='object'
                     AND jsonb_typeof(r->'kinds')='array'
                THEN ARRAY(SELECT k FROM jsonb_array_elements_text(r->'kinds') k
                            WHERE n.ontology->'kinds' ? k)
                ELSE ARRAY(SELECT jsonb_array_elements_text(n.ontology->'kinds')) END AS kinds,
           CASE WHEN jsonb_typeof(r)='object'
                     AND jsonb_typeof(r->'dst_kinds')='array'
                THEN ARRAY(SELECT k FROM jsonb_array_elements_text(r->'dst_kinds') k
                            WHERE n.ontology->'kinds' ? k)
                WHEN jsonb_typeof(r)='object'
                     AND jsonb_typeof(r->'target_kinds')='array'
                THEN ARRAY(SELECT k FROM jsonb_array_elements_text(r->'target_kinds') k
                            WHERE n.ontology->'kinds' ? k)
                WHEN jsonb_typeof(r)='object'
                     AND jsonb_typeof(r->'kinds')='array'
                THEN ARRAY(SELECT k FROM jsonb_array_elements_text(r->'kinds') k
                            WHERE n.ontology->'kinds' ? k)
                ELSE ARRAY(SELECT jsonb_array_elements_text(n.ontology->'kinds')) END AS dst_kinds
      FROM novel n CROSS JOIN LATERAL jsonb_array_elements(COALESCE(n.ontology->'relations','[]'::jsonb)) r
     WHERE n.id=p_novel_id
  )
  INSERT INTO novel_vocabulary
    (novel_id,term_type,name,kinds,dst_kinds,cardinality,polarity,status,gloss,
     proposals,first_seen_chapter,last_seen_chapter,admitted_at_chapter)
  SELECT p_novel_id,'relation',r.name,r.kinds,r.dst_kinds,'accretive',0,'admitted',
         'durable relationship; preserve the novel-specific evidence',0,0,0,0
    FROM rels r
    WHERE r.name ~ '^[a-z][a-z0-9_]{1,39}$'
      AND cardinality(r.kinds) > 0 AND cardinality(r.dst_kinds) > 0
  ON CONFLICT(novel_id,term_type,name) DO UPDATE SET
    kinds = ARRAY(SELECT DISTINCT k FROM unnest(novel_vocabulary.kinds || EXCLUDED.kinds) k ORDER BY k),
    dst_kinds = ARRAY(SELECT DISTINCT k FROM unnest(novel_vocabulary.dst_kinds || EXCLUDED.dst_kinds) k ORDER BY k),
    updated_at = now();
END
$$;

CREATE FUNCTION initialize_novel_vocabulary()
RETURNS TRIGGER
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public
AS $$
BEGIN
  PERFORM seed_novel_vocabulary(NEW.id);
  RETURN NEW;
END
$$;
CREATE TRIGGER initialize_novel_vocabulary
  AFTER INSERT ON novel
  FOR EACH ROW EXECUTE FUNCTION initialize_novel_vocabulary();

-- Seed every novel already present, then install the same path for future novels.
SELECT seed_novel_vocabulary(id) FROM novel;

-- Enforce same-novel supersession and reject a cycle in the append-only correction chain.
CREATE FUNCTION guard_edge_supersession()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE target_novel UUID; found_cycle BOOLEAN;
BEGIN
  IF NEW.supersedes IS NULL THEN
    RETURN NEW;
  END IF;
  SELECT e.novel_id INTO target_novel FROM edge e WHERE e.id=NEW.supersedes;
  IF NOT FOUND OR target_novel IS DISTINCT FROM NEW.novel_id THEN
    RAISE EXCEPTION 'edge supersession must target an edge in the same novel';
  END IF;
  WITH RECURSIVE chain(id) AS (
    SELECT NEW.supersedes
    UNION ALL
    SELECT e.supersedes FROM edge e JOIN chain c ON e.id=c.id
     WHERE e.supersedes IS NOT NULL
  )
  SELECT EXISTS (SELECT 1 FROM chain WHERE id=NEW.id) INTO found_cycle;
  IF found_cycle THEN
    RAISE EXCEPTION 'edge supersession cycle for edge %', NEW.id;
  END IF;
  RETURN NEW;
END
$$;
CREATE TRIGGER edge_supersession_guard
  BEFORE INSERT OR UPDATE ON edge
  FOR EACH ROW EXECUTE FUNCTION guard_edge_supersession();

GRANT SELECT ON novel_vocabulary, novel_vocabulary_chapter,
  novel_vocabulary_alias, novel_assertion_evidence, novel_vocabulary_changelog TO ingest_writer;
GRANT INSERT, UPDATE ON novel_vocabulary TO ingest_writer;
GRANT INSERT ON novel_vocabulary_chapter, novel_vocabulary_alias,
  novel_assertion_evidence, novel_vocabulary_changelog TO ingest_writer;
GRANT USAGE, SELECT ON SEQUENCE novel_vocabulary_alias_id_seq,
  novel_vocabulary_changelog_id_seq TO ingest_writer;
GRANT EXECUTE ON FUNCTION normalize_novel_vocabulary_name(TEXT),
  canonical_novel_vocabulary_name(UUID,TEXT,TEXT,INT) TO ingest_writer;
REVOKE EXECUTE ON FUNCTION seed_novel_vocabulary(UUID), initialize_novel_vocabulary(),
  guard_novel_vocabulary_alias_cycle(), guard_edge_supersession() FROM PUBLIC;

COMMIT;
