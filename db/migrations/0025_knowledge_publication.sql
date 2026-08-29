BEGIN;
CREATE FUNCTION validate_published_claim() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE r graph_revision; ev graph_evidence; knowledge int; eid uuid;
BEGIN
 SELECT * INTO r FROM graph_revision WHERE id=NEW.revision_id;
 IF r.legacy THEN RETURN NEW; END IF;
 IF TG_TABLE_NAME='entity' THEN
  IF NOT (r.ontology->'kinds' ? NEW.kind) THEN RAISE EXCEPTION 'invalid ontology kind'; END IF;
  RETURN NEW;
 END IF;
 SELECT * INTO ev FROM graph_evidence WHERE id=NEW.evidence_id AND revision_id=r.id;
 IF NOT FOUND THEN RAISE EXCEPTION 'missing revision evidence'; END IF;
 knowledge:=CASE WHEN TG_TABLE_NAME='alias' THEN NEW.first_seen_chapter ELSE 0 END;
 IF TG_TABLE_NAME='event' THEN
  knowledge:=NEW.chapter_index;
  FOREACH eid IN ARRAY NEW.entity_ids LOOP
   IF NOT EXISTS(SELECT 1 FROM entity e WHERE e.id=eid AND e.revision_id=r.id AND e.first_seen_chapter<=knowledge)
   THEN RAISE EXCEPTION 'invalid event endpoint'; END IF;
  END LOOP;
 ELSIF TG_TABLE_NAME IN ('fact','edge') THEN
  knowledge:=NEW.source_chapter;
 END IF;
 IF ev.chapter_index>knowledge THEN RAISE EXCEPTION 'claim precedes its evidence'; END IF;
 RETURN NEW;
END $$;
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['entity','alias','fact','edge','event'] LOOP
 EXECUTE format('CREATE TRIGGER validate_publication AFTER INSERT OR UPDATE ON %I FOR EACH ROW EXECUTE FUNCTION validate_published_claim()',t);
 END LOOP;
END $$;

-- Revision-specific glossary voting is independent of entity creation and approval.
-- Legacy extraction also gets a real set, instead of the old last_seen counter.
CREATE TABLE glossary_candidate_chapter (
 novel_id uuid NOT NULL REFERENCES novel(id), source_term text NOT NULL,
 target_term text NOT NULL, chapter_index int NOT NULL,
 PRIMARY KEY(novel_id,source_term,target_term,chapter_index)
);
INSERT INTO glossary_candidate_chapter
 SELECT novel_id,source_term,target_term,first_seen_chapter FROM glossary_candidate ON CONFLICT DO NOTHING;
INSERT INTO glossary_candidate_chapter
 SELECT novel_id,source_term,target_term,last_seen_chapter FROM glossary_candidate ON CONFLICT DO NOTHING;
COMMIT;
