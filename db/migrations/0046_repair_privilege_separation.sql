-- 0046_repair_privilege_separation.sql — close two security findings in the repair surface.
--
-- Both are the same mistake in different places: a boundary that existed only as a promise
-- in application code, where §0 asks for one the database enforces.
--
-- (A) reader_repair_failures returned graph_job.error / event_job.error -- freeform
--     exception text that can embed source prose or a connection string (see 0020, which
--     deliberately kept such text out of the durable failure history) -- to any rls_reader
--     caller. reader-api classified it into a safe class and never forwarded it, but the
--     GRANT was the exposure, not the Go code: the discipline lived in the caller, where
--     the next caller would not inherit it. Now the class is derived once, in Python, at
--     the moment the exception is raised, and stored beside the text. The text stays in
--     the database for operator debugging; no read path returns it.
--
-- (B) repair_preview returns source quotes from every snapshotted chapter regardless of
--     reading progress -- a deliberate and necessary exception, since a reviewer who could
--     only see quotes up to their own progress could not assess the claims they are
--     approving. It was granted to rls_reader, which askai also connects as, so the only
--     thing preventing a spoiler leak was an app-layer check in one Go handler. Now only
--     repair_operator may execute it, and reader-api reaches it through a pool that holds
--     that role and is used by exactly one store method.
BEGIN;

-- (A) -------------------------------------------------------------------------------

ALTER TABLE graph_job ADD COLUMN category TEXT;
ALTER TABLE event_job ADD COLUMN category TEXT;

-- Existing failures keep a NULL category and surface as 'unknown'. They cannot be
-- reclassified: the class was never derived for them, and re-deriving it from stored text
-- would mean parsing exactly the strings this change exists to stop reading. Same
-- limitation, and the same reasoning, as 0020's note that historical missing errors cannot
-- be reconstructed from the ledger.

DROP FUNCTION reader_repair_failures(UUID);

CREATE FUNCTION reader_repair_failures(novel UUID)
RETURNS TABLE(
  track         TEXT,
  chapter_index INT,
  attempts      INT,
  category      TEXT,
  retry_at      TIMESTAMPTZ,
  updated_at    TIMESTAMPTZ
)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  (SELECT 'graph', j.chapter_index, j.attempts, coalesce(j.category, 'unknown'),
          j.retry_at, j.updated_at
     FROM graph_job j
    WHERE j.revision_id = repair_subject_revision(novel, 'graph') AND j.state = 'failed'
    ORDER BY j.updated_at DESC LIMIT 20)
  UNION ALL
  (SELECT 'events', j.chapter_index, j.attempts, coalesce(j.category, 'unknown'),
          j.retry_at, j.updated_at
     FROM event_job j
    WHERE j.revision_id = repair_subject_revision(novel, 'events') AND j.state = 'failed'
    ORDER BY j.updated_at DESC LIMIT 20)
$$;

ALTER FUNCTION reader_repair_failures(UUID) OWNER TO repair_reporter;
GRANT EXECUTE ON FUNCTION reader_repair_failures(UUID) TO rls_reader;

-- (B) -------------------------------------------------------------------------------

-- Roles are cluster-wide while the migration ledger is database-local, so tolerate a role
-- provisioned by another database or deployment bootstrap (see 0007).
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'repair_operator') THEN
    CREATE ROLE repair_operator NOLOGIN;
  END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO repair_operator;

-- Postgres grants EXECUTE on a new function to PUBLIC by default, so until this REVOKE
-- every repair function was callable by every role and the explicit GRANTs in 0042-0045
-- decided nothing. repair_preview in particular -- which returns source quotes from every
-- snapshotted chapter, ignoring reading progress -- was reachable by any role including
-- rls_reader. Revoking PUBLIC is what makes the grants below actually mean something.
--
-- Only these seven are touched. The older reader_* helpers (reader_knowledge_status,
-- reader_event_status, reader_graph_revision, ...) are also PUBLIC-executable, but they
-- are safe by construction: each derives its scope from the app.novel_id / app.current_chapter
-- GUCs, so a caller that has not been given a reader context gets nothing back. The repair
-- functions take the novel as an explicit argument, which is precisely why PUBLIC matters
-- for them and not for those.
REVOKE EXECUTE ON FUNCTION
  repair_subject_revision(UUID, TEXT),
  repair_preview(UUID, TEXT),
  reader_repair_status(UUID),
  reader_repair_failures(UUID),
  reader_repair_requests(UUID),
  reader_repair_history(UUID),
  reader_repair_rollback_targets(UUID)
FROM PUBLIC;

-- repair_subject_revision gets no grant at all: it is a helper called only from inside the
-- definer functions above, which run as its owner.

GRANT EXECUTE ON FUNCTION
  reader_repair_status(UUID),
  reader_repair_failures(UUID),
  reader_repair_requests(UUID),
  reader_repair_history(UUID),
  reader_repair_rollback_targets(UUID)
TO rls_reader;

-- No BYPASSRLS and no table grants: this role needs nothing but the right to call one
-- SECURITY DEFINER function, which already runs as repair_reporter.
REVOKE EXECUTE ON FUNCTION repair_preview(UUID, TEXT) FROM rls_reader;
GRANT  EXECUTE ON FUNCTION repair_preview(UUID, TEXT) TO repair_operator;

-- HONEST LIMITATION, worth reading before trusting this.
--
-- In local development every service authenticates as the compose owner `engine`, and
-- least privilege is achieved only by the SET ROLE in reader-api's newRolePool and askai's
-- pool configure hook. `engine` is a superuser, and a superuser may SET ROLE to anything --
-- so in dev this separation documents intent rather than enforcing it.
--
-- It becomes a real boundary the moment each service logs in as a role that is a member of
-- only what it needs: reader-api's reader pool as a member of rls_reader alone, askai
-- likewise, and only reader-api's operator pool a member of repair_operator. .env.example
-- already states that expectation for rls_reader and reader_progress_writer; this adds a
-- third role to it. Provisioning those login roles is deployment work, not schema work,
-- and is deliberately not attempted here -- but no further code change is needed for it.

COMMIT;
