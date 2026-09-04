-- 0044_repair_preview.sql — read the frozen review report so review can happen in a browser.
--
-- graph_rebuild.preview builds the report (mention coverage, published claims with their
-- source quotes, rejected proposals, identity changes, evidence re-validation) and freezes
-- it into graph_revision.review guarded by the revision's version. event_rebuild.preview
-- does the same for event_revision. Both are writes, which is why the API cannot call
-- preview itself on a GET: the executor takes the report, this function reads it back.
--
-- IMPORTANT: unlike everything in 0042, this returns story content. The report embeds
-- source quotes from every chapter in the snapshot, with no regard for anyone's reading
-- progress — reviewing a graph is not reading a novel, and a reviewer who could only see
-- quotes up to their own progress could not assess the claims. So the caller MUST gate
-- this on operator authorization, which reader-api's getRepairPreview does. It is the one
-- deliberate spoiler-gate exception in the repair surface, and it exists because the
-- alternative is that review can only ever happen at a shell.
BEGIN;

CREATE FUNCTION repair_preview(novel UUID, track TEXT)
RETURNS TABLE(revision_id UUID, version BIGINT, state TEXT, review JSONB)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT r.id, r.version, r.state, r.review
    FROM graph_revision r
   WHERE track = 'graph' AND r.id = repair_subject_revision(novel, 'graph')
  UNION ALL
  SELECT r.id, r.version, r.state, r.review
    FROM event_revision r
   WHERE track = 'events' AND r.id = repair_subject_revision(novel, 'events')
$$;

ALTER FUNCTION repair_preview(UUID, TEXT) OWNER TO repair_reporter;
GRANT EXECUTE ON FUNCTION repair_preview(UUID, TEXT) TO rls_reader;

COMMIT;
