-- 0043_repair_request.sql — let the product ask for a repair, without moving any gate.
--
-- The whole repair lifecycle already exists and is well-defended: prepare quarantines and
-- snapshots, resume rebuilds chapter by chapter under a model pin, preview freezes a
-- report, record_review derives its metrics from per-item assessments joined against
-- actually-stored bindings, and switch is the only cutover. All of it lives in argparse
-- subcommands, so recovering a book means someone with a shell runs six commands.
--
-- The tempting fix — an HTTP handler that does the work — is wrong twice. Reimplementing
-- qualified()'s thresholds in Go duplicates a §0 gate in a second language where it will
-- drift (glossary_hash_test.go exists because that already happened once). Shelling out to
-- Python from a request handler puts unbounded model inference on the request path.
--
-- So this table is an INTENT RECORD, not a job queue. The API writes what was asked for;
-- pipeline/repair.py reads the row and calls the existing functions. Python remains the
-- single implementation of every gate, and the request path stays fast.
--
-- Deliberately not a Redis job kind: QueueMessage has no type field, is mirrored in Go,
-- and its Lua CLAIM script addresses KEYS by position (queue.py's own comment says so).
-- A chapter pointer is the only thing that queue understands. graph_job/event_job already
-- set the precedent for DB-backed work drained by the worker when readers are idle.
BEGIN;

CREATE TABLE repair_request (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  novel_id    UUID NOT NULL REFERENCES novel(id) ON DELETE CASCADE,
  track       TEXT NOT NULL CHECK (track IN ('graph', 'events')),
  -- resume is absent on purpose: once prepare has created the jobs, graph_rebuild's own
  -- drain_active already advances them on every idle worker tick. preview is absent too —
  -- the executor takes a report itself when a rebuild finishes, so review has one to
  -- match against. These four are the actions a human actually decides.
  action      TEXT NOT NULL CHECK (action IN ('prepare', 'review', 'activate', 'rollback')),
  -- No foreign key: 'graph' rows point at graph_revision and 'events' rows at
  -- event_revision, and one column cannot reference both. The executor resolves it
  -- against the right table for the track and fails the request if it does not exist.
  -- NULL for prepare, which is the action that creates a revision.
  revision_id UUID,
  -- Action arguments: model/provider for prepare, the review document for review, the
  -- report hash for activate. Shapes are validated by the executor, in Python, next to
  -- the functions that consume them.
  params      JSONB NOT NULL DEFAULT '{}',
  state       TEXT NOT NULL DEFAULT 'pending'
              CHECK (state IN ('pending', 'running', 'done', 'failed')),
  attempts    INT NOT NULL DEFAULT 0,
  retry_at    TIMESTAMPTZ,
  -- error is freeform and stays server-side; category is the safe class the API returns.
  -- Same split, and the same reason, as the graph_job failure ledger in 0042.
  error       TEXT,
  category    TEXT,
  result      JSONB,
  requested_by TEXT NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Concurrency control lives in Postgres, not in a Go mutex: one outstanding action per
-- novel per track. Two operators clicking "activate" at the same moment is a duplicate
-- key, not a race, and a second click while a rebuild is starting is refused with 409.
CREATE UNIQUE INDEX repair_request_one_active
  ON repair_request(novel_id, track) WHERE state IN ('pending', 'running');

CREATE INDEX repair_request_pending ON repair_request(state, created_at)
  WHERE state IN ('pending', 'running');

-- The executor runs as the pipeline's writer role.
GRANT SELECT, INSERT, UPDATE ON repair_request TO ingest_writer;
GRANT SELECT ON repair_request TO repair_reporter;

-- What the reader sees of the request queue: that something was asked for, by whom, and
-- how it went. params is NOT exposed — a review document is bulky and a prepare's model
-- choice is already reported as the replacement's model once the revision exists.
CREATE FUNCTION reader_repair_requests(novel UUID)
RETURNS TABLE(
  id           UUID,
  track        TEXT,
  action       TEXT,
  state        TEXT,
  attempts     INT,
  category     TEXT,
  requested_by TEXT,
  created_at   TIMESTAMPTZ,
  updated_at   TIMESTAMPTZ
)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT r.id, r.track, r.action, r.state, r.attempts, r.category,
         r.requested_by, r.created_at, r.updated_at
    FROM repair_request r
   WHERE r.novel_id = novel
   ORDER BY r.created_at DESC
   LIMIT 20
$$;

-- The audit trail: has this book been repaired before, and how often? graph_audit and
-- event_audit have recorded quarantine/review/activate/rollback all along; nothing has
-- ever read them. This is the answer to "does it keep getting corrupted".
CREATE FUNCTION reader_repair_history(novel UUID)
RETURNS TABLE(
  track      TEXT,
  action     TEXT,
  created_at TIMESTAMPTZ
)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  (SELECT 'graph', a.action, a.created_at FROM graph_audit a
    WHERE a.novel_id = novel ORDER BY a.created_at DESC LIMIT 25)
  UNION ALL
  (SELECT 'events', a.action, a.created_at FROM event_audit a
    WHERE a.novel_id = novel ORDER BY a.created_at DESC LIMIT 25)
$$;

GRANT SELECT ON graph_audit, event_audit TO repair_reporter;

ALTER FUNCTION reader_repair_requests(UUID) OWNER TO repair_reporter;
ALTER FUNCTION reader_repair_history(UUID) OWNER TO repair_reporter;
GRANT EXECUTE ON FUNCTION reader_repair_requests(UUID), reader_repair_history(UUID) TO rls_reader;

COMMIT;
