-- 0077_graph_per_chapter_publication.sql -- Phase E: per-chapter publication lifecycle
-- for the entity-graph track. The structured-event track (event_revision/event_rebuild.py)
-- keeps its existing 'review'/'activate' lifecycle untouched -- this only admits the two
-- new graph-track verbs alongside it, per the 0060/0064 restate pattern (repair_request's
-- action CHECK spans both tracks, so no existing value may be dropped).
BEGIN;

ALTER TABLE repair_request DROP CONSTRAINT repair_request_action_check;
ALTER TABLE repair_request ADD CONSTRAINT repair_request_action_check
  CHECK (action IN ('prepare','review','activate','rollback','reextract',
                    'reextract_apply','discard','extend','retry','adopt','quarantine'));

-- Both new verbs act on a specific revision, same shape as activate/rollback.
ALTER TABLE repair_request ADD CONSTRAINT repair_request_adopt_quarantine_scope
  CHECK (action NOT IN ('adopt','quarantine') OR revision_id IS NOT NULL);

-- reader_chapter_graph_extraction (originally 0061) only ever matched a STAGING revision,
-- because before Phase E the only way a chapter's extraction became visible was the
-- whole-revision review gate -- a staging chapter's status was the only thing worth
-- showing. Now a revision goes state='active' AND trusted the moment it is adopted, and
-- keeps taking new chapters (graph_job rows) from enqueue_completed/resume as the reader
-- advances, so an adopted revision's own in-progress chapter needs the same "extracted,
-- not yet visible as facts" signal a staging one always had. `provenance` says which case
-- produced the row, so the UI is not left guessing from state alone.
DROP FUNCTION reader_chapter_graph_extraction(UUID, INT);

CREATE FUNCTION reader_chapter_graph_extraction(requested_novel UUID, requested_chapter INT)
RETURNS TABLE(
  state               TEXT,
  provenance          TEXT,
  verified_terms      INT,
  verified_claims     INT,
  published_fact_rows INT
)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  WITH selected_revision AS (
    SELECT r.id, CASE WHEN r.state = 'staging' THEN 'staging' ELSE 'active' END AS provenance
      FROM graph_revision r
     WHERE requested_novel = reader_novel()
       AND requested_chapter <= reader_chapter()
       AND r.novel_id = requested_novel
       AND ((r.state = 'staging') OR (r.state = 'active' AND r.trusted))
     ORDER BY CASE WHEN r.state = 'staging' THEN 0 ELSE 1 END,
              r.created_at DESC, r.id DESC
     LIMIT 1
  )
  SELECT j.state, sr.provenance,
         coalesce((j.output->'diagnostics'->>'verified_identities')::int, 0),
         coalesce((j.output->'diagnostics'->>'verified_claims')::int, 0),
         coalesce((j.output->'diagnostics'->>'published_facts')::int, 0)
    FROM selected_revision sr
    JOIN graph_job j ON j.revision_id = sr.id AND j.chapter_index = requested_chapter
$$;

ALTER FUNCTION reader_chapter_graph_extraction(UUID, INT) OWNER TO repair_reporter;
REVOKE EXECUTE ON FUNCTION reader_chapter_graph_extraction(UUID, INT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION reader_chapter_graph_extraction(UUID, INT) TO rls_reader;

COMMIT;
