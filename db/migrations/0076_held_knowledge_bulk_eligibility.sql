-- 0076_held_knowledge_bulk_eligibility.sql — one shared corroboration rule for both the
-- review preview (reader-api, read-only) and the pass_all_corroborated write
-- (ingest-api). Phase D's plan requires showing the reviewer the exact selected
-- items/evidence before they approve a bulk pass; that is only safe if the preview and
-- the write can never disagree about which facts qualify.
--
-- corroborated_fact_ids is the single definition of B.2.5's rule (same assertion, same
-- normalized value, evidenced across >=2 distinct chapters, no conflicting signature and
-- no review flag anywhere in the group -- so e.g. "alive"/"dead" can never corroborate
-- each other). ingest-api's write path calls it directly (it already holds SELECT on
-- fact and novel_assertion_evidence as ingest_writer, and the caller has already locked
-- novel/revision/chapter). reader_held_knowledge_bulk_eligible wraps it with the same
-- revision-selection and chapter/novel authorization reader_held_knowledge (0074) uses,
-- so a reviewer can see the preview without a privilege reader-api's rls_reader
-- connection does not otherwise have (RLS hides held rows, and rls_reader has no grant
-- on novel_assertion_evidence at all).
BEGIN;

CREATE FUNCTION corroborated_fact_ids(p_novel UUID, p_revision UUID, p_chapter INT)
RETURNS TABLE(fact_id BIGINT)
LANGUAGE sql STABLE AS $$
  WITH visible AS (
    SELECT entity_id, attribute, assertion_signature, chapter_index, review_flag
      FROM novel_assertion_evidence
     WHERE novel_id = p_novel AND revision_id = p_revision AND chapter_index <= p_chapter
  ), groups AS (
    SELECT entity_id, attribute
      FROM visible
     GROUP BY entity_id, attribute
    HAVING count(DISTINCT assertion_signature) = 1
       AND count(DISTINCT chapter_index) >= 2
       AND bool_or(review_flag IS NOT NULL) = false
  )
  SELECT f.id
    FROM fact f
    JOIN groups g ON g.entity_id = f.entity_id AND g.attribute = f.attribute
   WHERE f.novel_id = p_novel AND f.revision_id = p_revision AND f.source_chapter = p_chapter
     AND f.review_state = 'held' AND f.kind = 'assertion'
$$;

-- Same PUBLIC-execute closure CLAUDE.md flags for every function added since 0046.
REVOKE EXECUTE ON FUNCTION corroborated_fact_ids(UUID, UUID, INT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION corroborated_fact_ids(UUID, UUID, INT) TO ingest_writer, repair_reporter;
GRANT SELECT ON novel_assertion_evidence TO repair_reporter;

CREATE FUNCTION reader_held_knowledge_bulk_eligible(requested_novel UUID, requested_chapter INT)
RETURNS TABLE(item_id BIGINT)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  WITH selected_revision AS (
    SELECT r.id
      FROM graph_revision r
     WHERE requested_novel = reader_novel()
       AND requested_chapter >= 0
       AND requested_chapter <= reader_chapter()
       AND r.novel_id = requested_novel
       AND ((r.state = 'staging') OR (r.state = 'active' AND r.trusted))
     ORDER BY CASE WHEN r.state = 'staging' THEN 0 ELSE 1 END,
              r.created_at DESC, r.id DESC
     LIMIT 1
  )
  SELECT c.fact_id
    FROM selected_revision sr,
         LATERAL corroborated_fact_ids(requested_novel, sr.id, requested_chapter) c
$$;

ALTER FUNCTION reader_held_knowledge_bulk_eligible(UUID, INT) OWNER TO repair_reporter;
REVOKE EXECUTE ON FUNCTION reader_held_knowledge_bulk_eligible(UUID, INT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION reader_held_knowledge_bulk_eligible(UUID, INT) TO rls_reader;

COMMIT;
