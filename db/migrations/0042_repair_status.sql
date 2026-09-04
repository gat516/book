-- 0042_repair_status.sql — make knowledge repair a legible product state (§0).
--
-- Quarantine already works: graph_rebuild.prepare sets graph_revision.trusted=false,
-- reader_graph_revision() then returns NULL, and every RESTRICTIVE policy bound to it
-- returns zero rows. What the system could not do is *say so*. reader_knowledge_status
-- reports the single word 'repair' for one chapter; nothing exposes how many claims are
-- being withheld, whether a replacement is being built, how far it has got, or why it
-- keeps failing. This migration adds the read model for that, and nothing else — no new
-- write path, no change to any gate.
--
-- Two functions rather than one because the shapes differ: a per-novel summary (one row
-- per track) and a bounded failure ledger (many rows).
BEGIN;

-- The summary must count claims the reader CANNOT see, which is precisely what RLS
-- exists to prevent. fact/entity/chapter_event all FORCE row level security (0002, 0039),
-- so the table owner is subject to it too. A dedicated BYPASSRLS role, owning these two
-- functions, is the narrow exception: it may count rows and read revision metadata, and
-- is granted SELECT on nothing else. It can never return quote text or claim values —
-- see the column lists below, which are deliberately all identifiers, counts and states.
-- Roles are cluster-wide while the migration ledger is database-local (see 0007).
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'repair_reporter') THEN
    CREATE ROLE repair_reporter NOLOGIN BYPASSRLS;
  END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO repair_reporter;
GRANT SELECT ON novel, fact, graph_revision, graph_job,
                chapter_event, event_revision, event_job TO repair_reporter;

-- Which revision the chapter counts and the failure ledger describe: the rebuild in
-- progress if there is one, otherwise the revision readers are gated to. Shared by both
-- functions below so the counts, the retry verdict and the failure list can never end up
-- describing different runs — an inconsistency the first draft of this migration had.
--
-- The fallback matters: a trusted, active graph whose chapter enrichment keeps erroring
-- is the same question to the person looking at the screen as a rebuild that will not
-- finish, and it is the only view of the "constantly getting corrupted" case that exists
-- before anyone decides to quarantine anything.
CREATE FUNCTION repair_subject_revision(novel UUID, track TEXT) RETURNS UUID
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT CASE WHEN track = 'graph' THEN
      coalesce(
        (SELECT r.id FROM graph_revision r
          WHERE r.novel_id = novel AND r.state = 'staging'
          ORDER BY r.created_at DESC LIMIT 1),
        (SELECT n.active_graph_revision FROM novel n WHERE n.id = novel))
    ELSE
      coalesce(
        (SELECT r.id FROM event_revision r
          WHERE r.novel_id = novel AND r.state = 'staging'
          ORDER BY r.created_at DESC LIMIT 1),
        (SELECT n.active_event_revision FROM novel n WHERE n.id = novel))
    END
$$;

-- One row per track ('graph', 'events'). The active revision is the one readers are
-- gated to; the replacement is the newest staging revision, which is what a rebuild in
-- progress writes into; the chapter counts describe the subject revision above, which is
-- whichever of those two is currently doing work. Deriving a human-readable state from
-- these numbers is the API's job, not this function's — same split as TranslationHealth,
-- where SQL reports and Go decides what to say about it.
CREATE FUNCTION reader_repair_status(novel UUID)
RETURNS TABLE(
  track               TEXT,
  active_revision     UUID,
  active_trusted      BOOLEAN,
  active_legacy       BOOLEAN,
  withheld_claims     BIGINT,
  subject_revision    UUID,
  replacement_id      UUID,
  replacement_model   TEXT,
  replacement_prompt  TEXT,
  replacement_created TIMESTAMPTZ,
  chapters_total      BIGINT,
  chapters_done       BIGINT,
  chapters_failed     BIGINT,
  chapters_running    BIGINT,
  activation_eligible BOOLEAN,
  review_hash         TEXT,
  retryable           BOOLEAN
)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
WITH graph_active AS (
  SELECT r.* FROM novel n JOIN graph_revision r ON r.id = n.active_graph_revision
   WHERE n.id = novel
), graph_staging AS (
  SELECT r.* FROM graph_revision r
   WHERE r.novel_id = novel AND r.state = 'staging'
   ORDER BY r.created_at DESC LIMIT 1
), event_active AS (
  SELECT r.* FROM novel n JOIN event_revision r ON r.id = n.active_event_revision
   WHERE n.id = novel
), event_staging AS (
  SELECT r.* FROM event_revision r
   WHERE r.novel_id = novel AND r.state = 'staging'
   ORDER BY r.created_at DESC LIMIT 1
)
-- LEFT JOIN from a constant so a track always reports exactly one row: a novel with no
-- event revision at all must still say so, rather than vanishing from the response.
SELECT 'graph',
       a.id, coalesce(a.trusted, false), coalesce(a.legacy, false),
       (SELECT count(*) FROM fact f WHERE f.revision_id = a.id),
       subject.id,
       s.id, s.model->>'name', s.prompt_version, s.created_at,
       (SELECT count(*) FROM graph_job j WHERE j.revision_id = subject.id),
       (SELECT count(*) FROM graph_job j WHERE j.revision_id = subject.id AND j.state = 'done'),
       (SELECT count(*) FROM graph_job j WHERE j.revision_id = subject.id AND j.state = 'failed'),
       (SELECT count(*) FROM graph_job j WHERE j.revision_id = subject.id AND j.state = 'processing'),
       (s.review->>'activation_eligible')::boolean,
       s.review->>'review_hash',
       -- Mirrors graph_rebuild.graph_retry_delay_minutes: past attempt 3 it returns None
       -- and retry_at is never set again, so the chapter is silently abandoned. That is
       -- the state this column exists to make visible.
       EXISTS(SELECT 1 FROM graph_job j
               WHERE j.revision_id = subject.id AND j.state = 'failed'
                 AND j.attempts <= 3 AND j.retry_at IS NOT NULL)
  FROM (SELECT repair_subject_revision(novel, 'graph') AS id) subject
  LEFT JOIN graph_active a ON true
  LEFT JOIN graph_staging s ON true
UNION ALL
SELECT 'events',
       a.id, coalesce(a.trusted, false), false,
       (SELECT count(*) FROM chapter_event e WHERE e.revision_id = a.id),
       subject.id,
       s.id, s.model->>'name', s.prompt_version, s.created_at,
       (SELECT count(*) FROM event_job j WHERE j.revision_id = subject.id),
       (SELECT count(*) FROM event_job j WHERE j.revision_id = subject.id AND j.state = 'done'),
       (SELECT count(*) FROM event_job j WHERE j.revision_id = subject.id AND j.state = 'failed'),
       (SELECT count(*) FROM event_job j WHERE j.revision_id = subject.id AND j.state = 'processing'),
       (s.review->>'activation_eligible')::boolean,
       s.review->>'review_hash',
       EXISTS(SELECT 1 FROM event_job j
               WHERE j.revision_id = subject.id AND j.state = 'failed'
                 AND j.attempts <= 3 AND j.retry_at IS NOT NULL)
  FROM (SELECT repair_subject_revision(novel, 'events') AS id) subject
  LEFT JOIN event_active a ON true
  LEFT JOIN event_staging s ON true
$$;

-- The failure ledger for the subject revision — the run the counts above describe. It
-- deliberately does NOT union every revision's failures: the same chapter failing on two
-- different revisions listed twice reads as two problems, and the attempt counts are
-- per-revision so they cannot be merged into one honest row.
--
-- error IS returned here, and the API must NOT pass it to a browser. It is freeform
-- exception text that can embed source prose or a connection string; migration 0020
-- deliberately kept such text out of the durable failure history for that reason. The
-- caller maps it to a safe category. It is exposed to the API only because classifying
-- an exception is far better done in testable application code than in SQL.
CREATE FUNCTION reader_repair_failures(novel UUID)
RETURNS TABLE(
  track         TEXT,
  chapter_index INT,
  attempts      INT,
  error         TEXT,
  retry_at      TIMESTAMPTZ,
  updated_at    TIMESTAMPTZ
)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  (SELECT 'graph', j.chapter_index, j.attempts, j.error, j.retry_at, j.updated_at
     FROM graph_job j
    WHERE j.revision_id = repair_subject_revision(novel, 'graph') AND j.state = 'failed'
    ORDER BY j.updated_at DESC LIMIT 20)
  UNION ALL
  (SELECT 'events', j.chapter_index, j.attempts, j.error, j.retry_at, j.updated_at
     FROM event_job j
    WHERE j.revision_id = repair_subject_revision(novel, 'events') AND j.state = 'failed'
    ORDER BY j.updated_at DESC LIMIT 20)
$$;

ALTER FUNCTION repair_subject_revision(UUID, TEXT) OWNER TO repair_reporter;
ALTER FUNCTION reader_repair_status(UUID) OWNER TO repair_reporter;
ALTER FUNCTION reader_repair_failures(UUID) OWNER TO repair_reporter;

-- reader-api connects as rls_reader; repair status is novel-level metadata, in the same
-- deliberately ungated class as GET /novels and GET /novels/{id}/pipeline. It reveals
-- nothing about story content — only that facts are being withheld, and how far the
-- machinery has got with replacing them.
GRANT EXECUTE ON FUNCTION reader_repair_status(UUID), reader_repair_failures(UUID) TO rls_reader;

COMMIT;
