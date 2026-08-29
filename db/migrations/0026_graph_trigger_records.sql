BEGIN;
CREATE OR REPLACE FUNCTION guard_graph_write() RETURNS trigger LANGUAGE plpgsql AS $$
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
   IF TG_TABLE_NAME IN ('fact','edge','event','alias') THEN
    IF NEW.evidence_id IS NULL THEN RAISE EXCEPTION 'published knowledge requires evidence'; END IF;
   END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE OR REPLACE FUNCTION validate_published_claim() RETURNS trigger LANGUAGE plpgsql AS $$
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
 knowledge:=0;
 IF TG_TABLE_NAME='alias' THEN knowledge:=NEW.first_seen_chapter; END IF;
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
COMMIT;
