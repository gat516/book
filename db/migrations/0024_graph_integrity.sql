BEGIN;
-- Revision membership is structural, including evidence and relationship endpoints.
ALTER TABLE graph_evidence ADD CONSTRAINT evidence_revision_unique UNIQUE(id,revision_id);
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['alias','fact','edge','event','source_mention','mention_binding','display_mention'] LOOP
 EXECUTE format('ALTER TABLE %I ADD CONSTRAINT %I FOREIGN KEY(evidence_id,revision_id) REFERENCES graph_evidence(id,revision_id)',t,t||'_evidence_revision');
 END LOOP;
END $$;
CREATE FUNCTION validate_graph_record() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE r graph_revision; ec int; source_ch int;
BEGIN
 SELECT * INTO r FROM graph_revision WHERE id=NEW.revision_id FOR SHARE;
 IF NOT FOUND THEN RAISE EXCEPTION 'unknown revision'; END IF;
 IF r.legacy THEN RETURN NEW; END IF;
 IF r.state='archived' OR NULLIF(current_setting('app.graph_revision',true),'')::uuid IS DISTINCT FROM r.id
 OR COALESCE(NULLIF(current_setting('app.graph_generation',true),''),'0')::bigint<>r.generation
 THEN RAISE EXCEPTION 'graph write fenced'; END IF;
 IF TG_TABLE_NAME IN ('source_mention','display_mention','graph_evidence') THEN
   IF NEW.novel_id<>r.novel_id THEN RAISE EXCEPTION 'cross novel record'; END IF;
 END IF;
 IF TG_TABLE_NAME='mention_binding' THEN
   SELECT chapter_index INTO source_ch FROM source_mention WHERE revision_id=r.id AND id=NEW.mention_id;
   SELECT chapter_index INTO ec FROM graph_evidence WHERE id=NEW.evidence_id;
   IF NEW.known_from_chapter<greatest(source_ch,ec) THEN RAISE EXCEPTION 'backdated identity evidence'; END IF;
 ELSIF TG_TABLE_NAME='graph_evidence' THEN
   IF NEW.source_hash IS DISTINCT FROM (SELECT c->>'source_hash' FROM jsonb_array_elements(r.snapshot->'chapters') c WHERE (c->>'chapter')::int=NEW.chapter_index)
   THEN RAISE EXCEPTION 'evidence outside revision snapshot'; END IF;
 END IF;
 RETURN NEW;
END $$;
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['graph_evidence','source_mention','mention_binding','display_mention','glossary_binding','glossary_proposal_chapter'] LOOP
 EXECUTE format('CREATE TRIGGER graph_integrity BEFORE INSERT OR UPDATE ON %I FOR EACH ROW EXECUTE FUNCTION validate_graph_record()',t);
 END LOOP;
END $$;

-- Empty legacy display spans are only presentation, and remain useful while repair
-- is withheld. No IDs or claims can pass this exception. Never rewrite prose.
CREATE FUNCTION allow_placeholder_revision() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.entity_id IS NULL AND NEW.revision_id IS NULL THEN
  SELECT id INTO NEW.revision_id FROM graph_revision WHERE novel_id=NEW.novel_id AND legacy ORDER BY created_at LIMIT 1;
 END IF;
 RETURN NEW;
END $$;
DROP TRIGGER graph_write_fence ON mention_span;
CREATE FUNCTION guard_mention_write() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE r graph_revision;
BEGIN
 SELECT * INTO r FROM graph_revision WHERE novel_id=NEW.novel_id AND legacy ORDER BY created_at LIMIT 1 FOR SHARE;
 NEW.revision_id:=r.id;
 IF NEW.entity_id IS NOT NULL AND (NOT r.trusted OR r.state<>'active') THEN RAISE EXCEPTION 'legacy link writer fenced'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER graph_write_fence BEFORE INSERT OR UPDATE ON mention_span FOR EACH ROW EXECUTE FUNCTION guard_mention_write();
COMMIT;
