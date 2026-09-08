-- 0067_graph_worker_progress.sql -- durable, content-free graph rebuild liveness.
--
-- A chapter can spend minutes inside one model call.  The job's old updated_at moved
-- only when the entire chapter changed state, so "working", "hung", and "worker died"
-- were indistinguishable.  These fields contain stage names and timestamps only: no
-- source text or unreviewed model output crosses the reader boundary (§0).
BEGIN;

ALTER TABLE graph_job ADD COLUMN current_stage TEXT;
ALTER TABLE graph_job ADD COLUMN stage_started_at TIMESTAMPTZ;
ALTER TABLE graph_job ADD COLUMN last_progress_at TIMESTAMPTZ;

CREATE FUNCTION reader_graph_worker_report(novel UUID)
RETURNS TABLE(
  job_state        TEXT,
  current_stage    TEXT,
  stage_started_at TIMESTAMPTZ,
  last_progress_at TIMESTAMPTZ,
  attempts         INT,
  category         TEXT,
  updated_at       TIMESTAMPTZ
)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT j.state,j.current_stage,j.stage_started_at,j.last_progress_at,
         j.attempts,j.category,j.updated_at
    FROM graph_revision r
    JOIN graph_job j ON j.revision_id=r.id
   WHERE r.novel_id=novel AND r.state='staging'
   ORDER BY CASE j.state
              WHEN 'processing' THEN 0 WHEN 'failed' THEN 1
              WHEN 'pending' THEN 2 ELSE 3 END,
            j.chapter_index
   LIMIT 1
$$;

ALTER FUNCTION reader_graph_worker_report(UUID) OWNER TO repair_reporter;
REVOKE EXECUTE ON FUNCTION reader_graph_worker_report(UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION reader_graph_worker_report(UUID) TO rls_reader;

COMMIT;
