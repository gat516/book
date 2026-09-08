-- 0074_held_knowledge.sql — human review authorization for extracted knowledge (§0.2, §0.3).
--
-- review_state is mutable authorization metadata, not knowledge. Existing rows deliberately
-- default to held: no historical extraction is grandfathered into reader visibility.
BEGIN;

ALTER TABLE fact
  ADD COLUMN review_state TEXT NOT NULL DEFAULT 'held'
    CHECK (review_state IN ('held','passed','rejected'));
ALTER TABLE edge
  ADD COLUMN review_state TEXT NOT NULL DEFAULT 'held'
    CHECK (review_state IN ('held','passed','rejected'));
ALTER TABLE event
  ADD COLUMN review_state TEXT NOT NULL DEFAULT 'held'
    CHECK (review_state IN ('held','passed','rejected'));

-- These policies must be RESTRICTIVE. A permissive policy would OR with the existing
-- novel/chapter gate and could bypass it; restrictive policies compose by subtraction
-- with quarantine and the revision fences (§0.3).
ALTER TABLE fact ENABLE ROW LEVEL SECURITY;
ALTER TABLE edge ENABLE ROW LEVEL SECURITY;
ALTER TABLE event ENABLE ROW LEVEL SECURITY;
CREATE POLICY review_fact ON fact AS RESTRICTIVE USING (review_state = 'passed');
CREATE POLICY review_edge ON edge AS RESTRICTIVE USING (review_state = 'passed');
CREATE POLICY review_event ON event AS RESTRICTIVE USING (review_state = 'passed');

CREATE INDEX fact_held_review_queue
  ON fact (novel_id, source_chapter, review_state)
  WHERE review_state = 'held';
CREATE INDEX edge_held_review_queue
  ON edge (novel_id, source_chapter, review_state)
  WHERE review_state = 'held';
CREATE INDEX event_held_review_queue
  ON event (novel_id, chapter_index, review_state)
  WHERE review_state = 'held';

-- The base tables have globally unique IDs, but the review action must also prove that
-- the selected row belongs to the locked novel and revision. These narrow keys support
-- composite FKs without weakening any existing FK or changing row identity.
ALTER TABLE fact ADD CONSTRAINT fact_id_novel_revision_key UNIQUE (id, novel_id, revision_id);
ALTER TABLE edge ADD CONSTRAINT edge_id_novel_revision_key UNIQUE (id, novel_id, revision_id);
ALTER TABLE event ADD CONSTRAINT event_id_novel_revision_key UNIQUE (id, novel_id, revision_id);

CREATE TABLE knowledge_review_audit (
  id BIGSERIAL PRIMARY KEY,
  novel_id UUID NOT NULL,
  revision_id UUID NOT NULL,
  fact_id BIGINT,
  edge_id BIGINT,
  event_id BIGINT,
  actor TEXT NOT NULL,
  old_state TEXT NOT NULL CHECK (old_state IN ('held','passed','rejected')),
  new_state TEXT NOT NULL CHECK (new_state IN ('held','passed','rejected')),
  action TEXT NOT NULL CHECK (action IN ('pass','reject','reset')),
  reason TEXT NOT NULL,
  request_id TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (num_nonnulls(fact_id, edge_id, event_id) = 1),
  UNIQUE (novel_id, idempotency_key),
  FOREIGN KEY (novel_id, revision_id)
    REFERENCES graph_revision(novel_id, id) ON DELETE CASCADE,
  FOREIGN KEY (fact_id, novel_id, revision_id)
    REFERENCES fact(id, novel_id, revision_id) ON DELETE CASCADE,
  FOREIGN KEY (edge_id, novel_id, revision_id)
    REFERENCES edge(id, novel_id, revision_id) ON DELETE CASCADE,
  FOREIGN KEY (event_id, novel_id, revision_id)
    REFERENCES event(id, novel_id, revision_id) ON DELETE CASCADE
);

-- Reviewers see held rows through this narrow, chapter-authorized surface. Plain event
-- rows are intentionally separate from the structured chapter_event lineage.
CREATE FUNCTION reader_held_knowledge(requested_novel UUID, requested_chapter INT)
RETURNS TABLE(
  item_type       TEXT,
  item_id         BIGINT,
  revision_id     UUID,
  revision_version BIGINT,
  chapter_index   INT,
  entity_id       UUID,
  src_id          UUID,
  dst_id          UUID,
  attribute       TEXT,
  rel_type        TEXT,
  value           TEXT,
  summary         TEXT,
  evidence_id     UUID,
  evidence_quote  TEXT,
  review_state    TEXT,
  review_flag     TEXT
)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
WITH selected_revision AS (
  SELECT r.id, r.version
    FROM graph_revision r
   WHERE requested_novel = reader_novel()
     AND requested_chapter >= 0
     AND requested_chapter <= reader_chapter()
     AND r.novel_id = requested_novel
     AND ((r.state = 'staging') OR (r.state = 'active' AND r.trusted))
   ORDER BY CASE WHEN r.state = 'staging' THEN 0 ELSE 1 END,
            r.created_at DESC, r.id DESC
   LIMIT 1
), fact_items AS (
  SELECT 'fact'::TEXT, f.id, f.revision_id, r.version, f.source_chapter,
         f.entity_id, NULL::UUID, NULL::UUID, f.attribute, NULL::TEXT,
         COALESCE(f.value_en, f.value), NULL::TEXT, f.evidence_id, ev.quote,
         f.review_state, NULL::TEXT
    FROM fact f
    JOIN selected_revision r ON r.id=f.revision_id
    JOIN entity e ON e.id=f.entity_id AND e.revision_id=f.revision_id
                 AND e.novel_id=f.novel_id
                 AND e.first_seen_chapter <= f.source_chapter
    LEFT JOIN graph_evidence ev ON ev.id=f.evidence_id
                               AND ev.revision_id=f.revision_id
                               AND ev.chapter_index <= f.source_chapter
   WHERE f.novel_id=requested_novel
     AND f.source_chapter <= requested_chapter
), edge_items AS (
  SELECT 'edge'::TEXT, e.id, e.revision_id, r.version, e.source_chapter,
         NULL::UUID, e.src_id, e.dst_id, NULL::TEXT, e.rel_type,
         NULL::TEXT, NULL::TEXT, e.evidence_id, ev.quote,
         e.review_state, NULL::TEXT
    FROM edge e
    JOIN selected_revision r ON r.id=e.revision_id
    JOIN entity src ON src.id=e.src_id AND src.revision_id=e.revision_id
                   AND src.novel_id=e.novel_id
                   AND src.first_seen_chapter <= e.source_chapter
    JOIN entity dst ON dst.id=e.dst_id AND dst.revision_id=e.revision_id
                   AND dst.novel_id=e.novel_id
                   AND dst.first_seen_chapter <= e.source_chapter
    LEFT JOIN graph_evidence ev ON ev.id=e.evidence_id
                               AND ev.revision_id=e.revision_id
                               AND ev.chapter_index <= e.source_chapter
   WHERE e.novel_id=requested_novel
     AND e.source_chapter <= requested_chapter
), event_items AS (
  SELECT 'event'::TEXT, e.id, e.revision_id, r.version, e.chapter_index,
         NULL::UUID, NULL::UUID, NULL::UUID, NULL::TEXT, NULL::TEXT,
         NULL::TEXT, e.summary, e.evidence_id, ev.quote,
         e.review_state, NULL::TEXT
    FROM event e
    JOIN selected_revision r ON r.id=e.revision_id
    LEFT JOIN graph_evidence ev ON ev.id=e.evidence_id
                               AND ev.revision_id=e.revision_id
                               AND ev.chapter_index <= e.chapter_index
   WHERE e.novel_id=requested_novel
     AND e.chapter_index <= requested_chapter
     AND NOT EXISTS (
       SELECT 1
         FROM unnest(e.entity_ids) AS ids(entity_id)
        WHERE NOT EXISTS (
          SELECT 1 FROM entity visible_entity
           WHERE visible_entity.id=ids.entity_id
             AND visible_entity.revision_id=e.revision_id
             AND visible_entity.novel_id=e.novel_id
             AND visible_entity.first_seen_chapter <= e.chapter_index
        )
     )
)
SELECT * FROM fact_items
UNION ALL SELECT * FROM edge_items
UNION ALL SELECT * FROM event_items
ORDER BY 5 DESC, 1, 2 DESC
$$;

ALTER FUNCTION reader_held_knowledge(UUID, INT) OWNER TO repair_reporter;
REVOKE EXECUTE ON FUNCTION reader_held_knowledge(UUID, INT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION reader_held_knowledge(UUID, INT) TO rls_reader;

GRANT SELECT ON fact, edge, event, entity, graph_evidence, graph_revision TO repair_reporter;
GRANT UPDATE(review_state) ON fact, edge, event TO ingest_writer;
GRANT INSERT ON knowledge_review_audit TO ingest_writer;
GRANT USAGE, SELECT ON SEQUENCE knowledge_review_audit_id_seq TO ingest_writer;

COMMIT;
