-- 0061_staging_graph_extraction_status.sql -- report completed staging extraction
-- without exposing unreviewed graph content.
--
-- Reader queries must remain fenced to the trusted active revision (§0). A chapter UI
-- still needs to distinguish "not extracted" from "extracted into the rebuild and
-- awaiting activation", so this function returns four aggregate fields and no claims,
-- terms, evidence, or model output. Novel identity and knowledge-time are checked from
-- the transaction's reader context inside the definer function.
BEGIN;

CREATE FUNCTION reader_chapter_graph_extraction(requested_novel UUID, requested_chapter INT)
RETURNS TABLE(
  state               TEXT,
  verified_terms      INT,
  verified_claims     INT,
  published_fact_rows INT
)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT j.state,
         coalesce((j.output->'diagnostics'->>'verified_identities')::int, 0),
         coalesce((j.output->'diagnostics'->>'verified_claims')::int, 0),
         coalesce((j.output->'diagnostics'->>'published_facts')::int, 0)
    FROM graph_revision r
    JOIN graph_job j ON j.revision_id = r.id
   WHERE requested_novel = reader_novel()
     AND requested_chapter <= reader_chapter()
     AND r.novel_id = requested_novel
     AND r.state = 'staging'
     AND j.chapter_index = requested_chapter
   ORDER BY r.created_at DESC
   LIMIT 1
$$;

ALTER FUNCTION reader_chapter_graph_extraction(UUID, INT) OWNER TO repair_reporter;
REVOKE EXECUTE ON FUNCTION reader_chapter_graph_extraction(UUID, INT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION reader_chapter_graph_extraction(UUID, INT) TO rls_reader;

COMMIT;
