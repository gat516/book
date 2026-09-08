-- 0065_repair_request_retry_at.sql -- expose repair_request.retry_at to reader-api.
--
-- reader_repair_requests() already tells a reader a request is 'pending' with N attempts,
-- but not WHEN the worker will try again -- the exact gap that made a repair_request stuck
-- in a timeout/backoff cycle look identical, from the browser, to one healthily waiting
-- for its first attempt. Adding retry_at lets the UI say "retrying at 3:13 PM" instead of
-- rendering the same static "starting" sentence through every attempt.
BEGIN;

DROP FUNCTION reader_repair_requests(UUID);

CREATE FUNCTION reader_repair_requests(novel UUID)
RETURNS TABLE(
  id           UUID,
  track        TEXT,
  action       TEXT,
  state        TEXT,
  attempts     INT,
  category     TEXT,
  retry_at     TIMESTAMPTZ,
  requested_by TEXT,
  created_at   TIMESTAMPTZ,
  updated_at   TIMESTAMPTZ
)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT r.id, r.track, r.action, r.state, r.attempts, r.category, r.retry_at,
         r.requested_by, r.created_at, r.updated_at
    FROM repair_request r
   WHERE r.novel_id = novel
   ORDER BY r.created_at DESC
   LIMIT 20
$$;

ALTER FUNCTION reader_repair_requests(UUID) OWNER TO repair_reporter;
GRANT EXECUTE ON FUNCTION reader_repair_requests(UUID) TO rls_reader;

COMMIT;
