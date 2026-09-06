-- 0057_repair_progress_gloss.sql — repair_progress (0047) missed the same treatment
-- 0051 gave every reader-facing read path: it still returns raw f.value, so a fact
-- flipped from English (while "proposed") back to Chinese the moment its chapter
-- published and it moved into "Facts published". Same COALESCE discipline as
-- store.go/knowledge.go and repair_proposed_claims.
BEGIN;

CREATE OR REPLACE FUNCTION repair_progress(novel UUID)
RETURNS TABLE(
  fact_id       BIGINT,
  entity        TEXT,
  kind          TEXT,
  attribute     TEXT,
  value         TEXT,
  chapter_index INT,
  quote         TEXT
)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT f.id, e.canonical, e.kind, f.attribute, coalesce(f.value_en, f.value), f.source_chapter, ev.quote
    FROM fact f
    JOIN entity e ON e.id = f.entity_id
    LEFT JOIN graph_evidence ev ON ev.id = f.evidence_id
   WHERE f.revision_id = repair_subject_revision(novel, 'graph')
   ORDER BY f.id DESC
   LIMIT 30
$$;

ALTER FUNCTION repair_progress(UUID) OWNER TO repair_reporter;
REVOKE EXECUTE ON FUNCTION repair_progress(UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION repair_progress(UUID) TO repair_operator;

COMMIT;
