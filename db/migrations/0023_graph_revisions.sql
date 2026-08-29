-- Knowledge-time authorization and isolated, reviewable graph rebuilds (§0).
BEGIN;
CREATE TABLE graph_revision (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 novel_id uuid NOT NULL REFERENCES novel(id),
 state text NOT NULL DEFAULT 'staging' CHECK(state IN ('staging','active','archived')),
 trusted boolean NOT NULL DEFAULT false,
 legacy boolean NOT NULL DEFAULT false,
 generation bigint NOT NULL DEFAULT 1,
 version bigint NOT NULL DEFAULT 1,
 snapshot jsonb NOT NULL DEFAULT '{}',
 ontology jsonb NOT NULL,
 model jsonb NOT NULL DEFAULT '{}',
 prompt_version text NOT NULL DEFAULT 'evidence-v1',
 evaluation jsonb NOT NULL DEFAULT '{}',
 review jsonb,
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(novel_id,id)
);
CREATE UNIQUE INDEX graph_one_active ON graph_revision(novel_id) WHERE state='active';
ALTER TABLE novel ADD COLUMN active_graph_revision uuid REFERENCES graph_revision(id);
INSERT INTO graph_revision(novel_id,state,trusted,legacy,ontology)
 SELECT id,'active',true,true,ontology FROM novel;
UPDATE novel n SET active_graph_revision=r.id FROM graph_revision r WHERE r.novel_id=n.id;
CREATE FUNCTION initialize_graph_revision() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 INSERT INTO graph_revision(novel_id,state,trusted,legacy,ontology)
 VALUES(NEW.id,'active',true,true,NEW.ontology) RETURNING id INTO NEW.active_graph_revision;
 UPDATE novel SET active_graph_revision=NEW.active_graph_revision WHERE id=NEW.id;
 RETURN NEW;
END $$;
CREATE TRIGGER initialize_graph_revision AFTER INSERT ON novel FOR EACH ROW EXECUTE FUNCTION initialize_graph_revision();

CREATE TABLE graph_audit (
 id bigserial PRIMARY KEY, novel_id uuid NOT NULL REFERENCES novel(id),
 revision_id uuid NOT NULL REFERENCES graph_revision(id), action text NOT NULL,
 detail jsonb NOT NULL DEFAULT '{}', created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE graph_evidence (
 id uuid PRIMARY KEY, revision_id uuid NOT NULL REFERENCES graph_revision(id),
 novel_id uuid NOT NULL REFERENCES novel(id), chapter_index int NOT NULL,
 source_hash text NOT NULL, char_start int NOT NULL CHECK(char_start>=0),
 char_end int NOT NULL, quote text NOT NULL,
 CHECK(char_end>char_start AND char_length(quote)=char_end-char_start),
 UNIQUE(revision_id,chapter_index,source_hash,char_start,char_end)
);
ALTER TABLE entity ADD COLUMN revision_id uuid REFERENCES graph_revision(id);
ALTER TABLE alias ADD COLUMN revision_id uuid REFERENCES graph_revision(id);
ALTER TABLE fact ADD COLUMN revision_id uuid REFERENCES graph_revision(id);
ALTER TABLE edge ADD COLUMN revision_id uuid REFERENCES graph_revision(id);
ALTER TABLE event ADD COLUMN revision_id uuid REFERENCES graph_revision(id);
ALTER TABLE mention_span ADD COLUMN revision_id uuid REFERENCES graph_revision(id);
ALTER TABLE job ADD COLUMN revision_id uuid REFERENCES graph_revision(id);
UPDATE entity e SET revision_id=n.active_graph_revision FROM novel n WHERE n.id=e.novel_id;
UPDATE alias a SET revision_id=e.revision_id FROM entity e WHERE e.id=a.entity_id;
UPDATE fact f SET revision_id=n.active_graph_revision FROM novel n WHERE n.id=f.novel_id;
UPDATE edge e SET revision_id=n.active_graph_revision FROM novel n WHERE n.id=e.novel_id;
UPDATE event e SET revision_id=n.active_graph_revision FROM novel n WHERE n.id=e.novel_id;
UPDATE mention_span m SET revision_id=n.active_graph_revision FROM novel n WHERE n.id=m.novel_id;
UPDATE job j SET revision_id=n.active_graph_revision FROM novel n WHERE n.id=j.novel_id;
ALTER TABLE entity ALTER COLUMN revision_id SET NOT NULL;
ALTER TABLE alias ALTER COLUMN revision_id SET NOT NULL;
ALTER TABLE fact ALTER COLUMN revision_id SET NOT NULL;
ALTER TABLE edge ALTER COLUMN revision_id SET NOT NULL;
ALTER TABLE event ALTER COLUMN revision_id SET NOT NULL;
ALTER TABLE mention_span ALTER COLUMN revision_id SET NOT NULL;
ALTER TABLE entity ADD CONSTRAINT entity_revision_unique UNIQUE(id,revision_id);
ALTER TABLE alias ADD CONSTRAINT alias_same_revision FOREIGN KEY(entity_id,revision_id) REFERENCES entity(id,revision_id);
ALTER TABLE fact ADD CONSTRAINT fact_same_revision FOREIGN KEY(entity_id,revision_id) REFERENCES entity(id,revision_id);
ALTER TABLE edge ADD CONSTRAINT edge_src_revision FOREIGN KEY(src_id,revision_id) REFERENCES entity(id,revision_id);
ALTER TABLE edge ADD CONSTRAINT edge_dst_revision FOREIGN KEY(dst_id,revision_id) REFERENCES entity(id,revision_id);
ALTER TABLE fact ADD COLUMN evidence_id uuid REFERENCES graph_evidence(id);
ALTER TABLE edge ADD COLUMN evidence_id uuid REFERENCES graph_evidence(id);
ALTER TABLE event ADD COLUMN evidence_id uuid REFERENCES graph_evidence(id);
ALTER TABLE alias ADD COLUMN evidence_id uuid REFERENCES graph_evidence(id);
ALTER TABLE fact ADD COLUMN claim_key text;
ALTER TABLE edge ADD COLUMN claim_key text;
ALTER TABLE event ADD COLUMN claim_key text;
CREATE UNIQUE INDEX fact_claim_once ON fact(revision_id,claim_key);
CREATE UNIQUE INDEX edge_claim_once ON edge(revision_id,claim_key);
CREATE UNIQUE INDEX event_claim_once ON event(revision_id,claim_key);

CREATE TABLE source_mention (
 id uuid NOT NULL, revision_id uuid NOT NULL REFERENCES graph_revision(id),
 novel_id uuid NOT NULL REFERENCES novel(id), chapter_index int NOT NULL,
 surface text NOT NULL, kind text NOT NULL, evidence_id uuid NOT NULL REFERENCES graph_evidence(id),
 PRIMARY KEY(revision_id,id)
);
CREATE TABLE mention_binding (
 revision_id uuid NOT NULL, mention_id uuid NOT NULL,
 known_from_chapter int NOT NULL, entity_id uuid NOT NULL,
 evidence_id uuid NOT NULL REFERENCES graph_evidence(id),
 PRIMARY KEY(revision_id,mention_id,known_from_chapter),
 FOREIGN KEY(revision_id,mention_id) REFERENCES source_mention(revision_id,id),
 FOREIGN KEY(entity_id,revision_id) REFERENCES entity(id,revision_id)
);
CREATE TABLE display_mention (
 id uuid NOT NULL, revision_id uuid NOT NULL REFERENCES graph_revision(id),
 novel_id uuid NOT NULL REFERENCES novel(id), chapter_index int NOT NULL,
 mention_id uuid, char_start int NOT NULL CHECK(char_start>=0), char_end int NOT NULL,
 phrase text NOT NULL, display_hash text NOT NULL, evidence_id uuid REFERENCES graph_evidence(id),
 CHECK(char_end>char_start AND char_length(phrase)=char_end-char_start),
 PRIMARY KEY(revision_id,id),
 FOREIGN KEY(revision_id,mention_id) REFERENCES source_mention(revision_id,id)
);
CREATE TABLE graph_job (
 revision_id uuid NOT NULL REFERENCES graph_revision(id), chapter_index int NOT NULL,
 state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','processing','done','failed')),
 input_hash text NOT NULL, model_identity text NOT NULL,
 attempts int NOT NULL DEFAULT 0, generation bigint NOT NULL, error text,
 output jsonb, updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(revision_id,chapter_index)
);
CREATE TABLE graph_completion (
 revision_id uuid NOT NULL REFERENCES graph_revision(id), cache_key text NOT NULL,
 served_provider text NOT NULL CHECK(served_provider='ollama'), served_model text NOT NULL,
 response jsonb NOT NULL, PRIMARY KEY(revision_id,cache_key,served_provider,served_model)
);
CREATE TABLE glossary_binding (
 novel_id uuid NOT NULL, source_term text NOT NULL, revision_id uuid NOT NULL REFERENCES graph_revision(id),
 entity_id uuid NOT NULL, known_from_chapter int NOT NULL,
 PRIMARY KEY(novel_id,source_term,revision_id),
 FOREIGN KEY(entity_id,revision_id) REFERENCES entity(id,revision_id)
);
INSERT INTO glossary_binding SELECT g.novel_id,g.source_term,e.revision_id,g.entity_id,g.locked_at_chapter
 FROM glossary g JOIN entity e ON e.id=g.entity_id;
CREATE TABLE glossary_proposal_chapter (
 revision_id uuid NOT NULL REFERENCES graph_revision(id), source_term text NOT NULL,
 target_term text NOT NULL, chapter_index int NOT NULL, evidence_id uuid NOT NULL REFERENCES graph_evidence(id),
 PRIMARY KEY(revision_id,source_term,target_term,chapter_index)
);

-- A transaction must own a live generation before publishing new graph knowledge.
-- FOR SHARE serializes publication with activation/quarantine's FOR UPDATE fence.
CREATE FUNCTION guard_graph_write() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE r graph_revision; nid uuid; rid uuid;
BEGIN
 IF TG_TABLE_NAME='alias' THEN SELECT novel_id INTO nid FROM entity WHERE id=NEW.entity_id;
 ELSE nid:=NEW.novel_id; END IF;
 rid:=NULLIF(current_setting('app.graph_revision',true),'')::uuid;
 IF NEW.revision_id IS NULL THEN
   NEW.revision_id:=COALESCE(rid,(SELECT id FROM graph_revision WHERE novel_id=nid AND legacy ORDER BY created_at LIMIT 1));
 END IF;
 SELECT * INTO r FROM graph_revision WHERE id=NEW.revision_id AND novel_id=nid FOR SHARE;
 IF NOT FOUND OR r.state='archived' THEN RAISE EXCEPTION 'graph revision is fenced'; END IF;
 IF rid IS NULL THEN
   IF NOT r.legacy OR NOT r.trusted OR r.state<>'active' THEN RAISE EXCEPTION 'legacy graph writer is fenced'; END IF;
 ELSE
   IF rid<>r.id OR COALESCE(NULLIF(current_setting('app.graph_generation',true),''),'0')::bigint<>r.generation
   THEN RAISE EXCEPTION 'stale graph generation'; END IF;
   IF TG_TABLE_NAME IN ('fact','edge','event','alias') AND NEW.evidence_id IS NULL
   THEN RAISE EXCEPTION 'published knowledge requires evidence'; END IF;
 END IF;
 RETURN NEW;
END $$;
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['entity','alias','fact','edge','event','mention_span'] LOOP
 EXECUTE format('CREATE TRIGGER graph_write_fence BEFORE INSERT OR UPDATE ON %I FOR EACH ROW EXECUTE FUNCTION guard_graph_write()',t);
 END LOOP;
END $$;

-- Caller-set revision IDs can never opt a reader into a staging/archived graph.
CREATE FUNCTION reader_graph_revision() RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER SET search_path=public AS $$
 SELECT r.id FROM novel n JOIN graph_revision r ON r.id=n.active_graph_revision
 WHERE n.id=reader_novel() AND r.state='active' AND r.trusted
$$;
CREATE POLICY revision_entity ON entity AS RESTRICTIVE USING(revision_id=reader_graph_revision());
CREATE POLICY revision_alias ON alias AS RESTRICTIVE USING(revision_id=reader_graph_revision());
CREATE POLICY revision_fact ON fact AS RESTRICTIVE USING(revision_id=reader_graph_revision() AND entity_id IN(SELECT id FROM entity));
CREATE POLICY revision_edge ON edge AS RESTRICTIVE USING(revision_id=reader_graph_revision() AND src_id IN(SELECT id FROM entity) AND dst_id IN(SELECT id FROM entity));
CREATE POLICY revision_event ON event AS RESTRICTIVE USING(revision_id=reader_graph_revision() AND NOT EXISTS(SELECT 1 FROM unnest(entity_ids) eid WHERE eid NOT IN(SELECT id FROM entity)));
-- Legacy empty highlights remain available during quarantine, but API must join the
-- gated entity table rather than exposing the stored, potentially contaminated ID.
DO $$ DECLARE t text; gate text; BEGIN
 FOREACH t IN ARRAY ARRAY['graph_evidence','source_mention','display_mention','mention_binding','glossary_binding'] LOOP
 EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',t);
 EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',t);
 gate:=CASE WHEN t IN ('mention_binding','glossary_binding') THEN 'known_from_chapter' ELSE 'chapter_index' END;
 EXECUTE format('CREATE POLICY graph_gate ON %I USING(revision_id=reader_graph_revision() AND %I<=reader_chapter())',t,gate);
 EXECUTE format('GRANT SELECT ON %I TO rls_reader',t);
 END LOOP;
END $$;
CREATE FUNCTION reader_knowledge_status(ch int)
RETURNS TABLE(revision_id uuid,version bigint,trusted boolean,status text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=public AS $$
 SELECT r.id,r.version,r.trusted,
 CASE WHEN NOT r.trusted THEN 'repair' WHEN r.legacy THEN 'ready' ELSE COALESCE(j.state,'pending') END
 FROM novel n JOIN graph_revision r ON r.id=n.active_graph_revision
 LEFT JOIN graph_job j ON j.revision_id=r.id AND j.chapter_index=ch
 WHERE n.id=reader_novel() AND ch<=reader_chapter()
$$;
CREATE INDEX graph_evidence_chapter ON graph_evidence(revision_id,chapter_index);
CREATE INDEX source_mentions_chapter ON source_mention(revision_id,chapter_index);
CREATE INDEX display_mentions_chapter ON display_mention(revision_id,chapter_index);
CREATE INDEX entities_revision ON entity(revision_id,first_seen_chapter);
COMMIT;
