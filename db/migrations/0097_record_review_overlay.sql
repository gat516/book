-- 0097_record_review_overlay.sql -- append-only human review over immutable rows.
BEGIN;

ALTER TABLE record_row
  ADD CONSTRAINT record_row_generation_scope_uq UNIQUE (id, novel_id, generation_id);

CREATE TABLE record_review_decision (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  novel_id UUID NOT NULL,
  generation_id UUID NOT NULL,
  row_id UUID NOT NULL,
  source_chapter INT NOT NULL CHECK (source_chapter >= 0),
  decision TEXT NOT NULL CHECK (decision IN ('accepted','rejected')),
  actor TEXT NOT NULL,
  reason TEXT NOT NULL,
  request_id TEXT NOT NULL,
  request_fingerprint TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (novel_id, request_id),
  FOREIGN KEY (generation_id, novel_id)
    REFERENCES record_generation(id, novel_id) ON DELETE CASCADE,
  FOREIGN KEY (row_id, novel_id, generation_id)
    REFERENCES record_row(id, novel_id, generation_id) ON DELETE CASCADE
);
CREATE INDEX record_review_latest
  ON record_review_decision(novel_id, generation_id, row_id, created_at DESC, id DESC);

CREATE FUNCTION record_row_not_rejected(requested_row UUID, requested_generation UUID)
RETURNS BOOLEAN LANGUAGE sql STABLE AS $$
  SELECT COALESCE((
    SELECT d.decision <> 'rejected'
      FROM record_review_decision d
     WHERE d.row_id = requested_row AND d.generation_id = requested_generation
     ORDER BY d.created_at DESC, d.id DESC LIMIT 1
  ), true)
$$;

-- RLS on record_row hides a rejected identity before entity RLS can inspect it. This
-- narrow definer helper avoids that recursion while retaining the outer generation gate.
CREATE FUNCTION record_entity_not_rejected(requested_entity UUID, requested_generation UUID)
RETURNS BOOLEAN LANGUAGE sql STABLE SECURITY DEFINER SET search_path=public AS $$
  SELECT NOT EXISTS (
    SELECT 1 FROM record_participant p JOIN record_row w ON w.id=p.row_id
     WHERE p.entity_id=requested_entity AND w.generation_id=requested_generation
       AND w.record_type='IDENTITY' AND NOT record_row_not_rejected(w.id,w.generation_id)
  )
$$;
ALTER FUNCTION record_entity_not_rejected(UUID, UUID) OWNER TO repair_reporter;
REVOKE EXECUTE ON FUNCTION record_entity_not_rejected(UUID, UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION record_entity_not_rejected(UUID, UUID) TO rls_reader;
GRANT EXECUTE ON FUNCTION record_row_not_rejected(UUID, UUID) TO repair_reporter;
REVOKE EXECUTE ON FUNCTION record_row_not_rejected(UUID, UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION record_row_not_rejected(UUID, UUID) TO rls_reader, ingest_writer;

ALTER TABLE record_review_decision ENABLE ROW LEVEL SECURITY;
ALTER TABLE record_review_decision FORCE ROW LEVEL SECURITY;
CREATE POLICY record_review_gate ON record_review_decision AS RESTRICTIVE USING (
  novel_id=reader_novel()
  AND generation_id=(SELECT active_record_generation FROM novel WHERE id=reader_novel())
  AND source_chapter<=reader_chapter()
  AND EXISTS (SELECT 1 FROM record_run r WHERE r.novel_id=record_review_decision.novel_id
    AND r.generation_id=record_review_decision.generation_id
    AND r.chapter_index=record_review_decision.source_chapter AND r.status='published')
);
GRANT SELECT ON record_review_decision TO rls_reader;
GRANT SELECT,INSERT ON record_review_decision TO ingest_writer;

CREATE POLICY record_row_review_gate ON record_row AS RESTRICTIVE USING (
  record_row_not_rejected(id,generation_id)
);
CREATE POLICY record_entity_review_gate ON entity AS RESTRICTIVE USING (
  record_entity_not_rejected(entity.id,entity.record_generation_id)
);

-- Review needs to show a rejected row so an operator can undo it. Return a narrowly
-- gated JSON projection from a BYPASSRLS owner; ordinary record queries remain subject
-- to record_row_review_gate. Child data is included here to avoid re-entering RLS while
-- hydrating a deliberately hidden row.
CREATE FUNCTION reader_record_review_rows(requested_novel UUID, requested_generation UUID, requested_chapter INT)
RETURNS TABLE (row_id UUID, record_type TEXT, original_index INT, source_chapter INT,
  valid_from_chapter INT, temporal_qualifier TEXT, values_json JSONB,
  participants_json JSONB, evidence_json JSONB)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=public AS $$
  SELECT w.id,w.record_type,w.original_index,w.source_chapter,w.valid_from_chapter,w.temporal_qualifier,
    COALESCE((SELECT jsonb_agg(jsonb_build_object('field',v.field_name,'source',v.source_value,
      'rendered',COALESCE(rr.target_value,''),'render_status',COALESCE(rr.status,'pending')) ORDER BY v.field_name)
      FROM record_value v LEFT JOIN record_rendering rr ON rr.row_id=v.row_id AND rr.field_name=v.field_name
      WHERE v.row_id=w.id),'[]'::jsonb),
    COALESCE((SELECT jsonb_agg(jsonb_build_object('field',p.field_name,'surface',p.surface,
      'entity_id',p.entity_id,'reference_id',p.reference_id) ORDER BY p.ordinal)
      FROM record_participant p WHERE p.row_id=w.id),'[]'::jsonb),
    COALESCE((SELECT jsonb_agg(jsonb_build_object('passage_id',e.passage_id,'quote',e.quote,
      'text',p.text,'char_start',p.char_start,'char_end',p.char_end,'ordinal',p.ordinal) ORDER BY p.ordinal)
      FROM record_evidence e JOIN record_passage p ON p.run_id=e.run_id AND p.passage_id=e.passage_id
      WHERE e.row_id=w.id),'[]'::jsonb)
  FROM record_row w JOIN record_run r ON r.id=w.run_id AND r.novel_id=w.novel_id
    JOIN novel n ON n.id=w.novel_id
  WHERE requested_novel=reader_novel() AND requested_generation=n.active_record_generation
    AND w.novel_id=requested_novel AND w.generation_id=requested_generation
    AND w.source_chapter=requested_chapter AND requested_chapter<=reader_chapter()
    AND r.status='published' AND r.chapter_index=requested_chapter
  ORDER BY w.original_index LIMIT 500
$$;
ALTER FUNCTION reader_record_review_rows(UUID,UUID,INT) OWNER TO repair_reporter;
REVOKE EXECUTE ON FUNCTION reader_record_review_rows(UUID,UUID,INT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION reader_record_review_rows(UUID,UUID,INT) TO rls_reader;

COMMIT;
