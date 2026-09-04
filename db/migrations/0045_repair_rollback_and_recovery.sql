-- 0045_repair_rollback_and_recovery.sql — fixes found reviewing 0042-0044.
--
-- Three problems, all of them cases where the UI could not express what the underlying
-- functions actually require:
--
-- 1. Rollback needs an ARCHIVED revision (graph_rebuild.switch: "rollback target must be
--    an archived revision"), and nothing exposed the archived ones. The panel passed the
--    active revision, so every rollback failed. Hence reader_repair_rollback_targets.
--
-- 2. record_review sets review=NULL, so between recording a review and the worker taking
--    a fresh report there is a window where the operator has reviewed a rebuild but the
--    activation hash does not exist yet. Without a `reviewed` flag that window is
--    indistinguishable from "not reviewed", and the UI could only make the button vanish.
--
-- 3. Running prepare again is the documented way to restart after an exhausted rebuild,
--    so multiple staging revisions are legitimate — but only the newest was ever visible,
--    which silently hid how many times a book had been restarted. `superseded` is the
--    "does this keep getting corrupted" number for the rebuild itself.
--
-- Plus a durable claim timestamp on repair_request, so a worker that dies mid-action does
-- not brick repair for that book forever.
BEGIN;

-- A crashed worker leaves state='running', and repair_request_one_active covers
-- ('pending','running'), so every later request for that novel and track is refused with
-- 409 and nothing ever clears it. graph_job survives the same crash because
-- next_retryable_active_revision treats 'processing' as retryable; this column is how the
-- repair executor gets the equivalent.
ALTER TABLE repair_request ADD COLUMN started_at TIMESTAMPTZ;

DROP FUNCTION reader_repair_status(UUID);

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
  retryable           BOOLEAN,
  -- True once record_review has stored derived metrics on the replacement.  Distinguishes
  -- "reviewed, waiting for a fresh report" from "not reviewed yet"; without it the two
  -- look identical because record_review clears `review`.
  reviewed            BOOLEAN,
  -- Staging revisions for this track beyond the newest: earlier rebuild attempts that a
  -- later prepare superseded.
  superseded          BIGINT
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
       EXISTS(SELECT 1 FROM graph_job j
               WHERE j.revision_id = subject.id AND j.state = 'failed'
                 AND j.attempts <= 3 AND j.retry_at IS NOT NULL),
       coalesce(s.evaluation, '{}'::jsonb) <> '{}'::jsonb,
       greatest((SELECT count(*) FROM graph_revision r
                  WHERE r.novel_id = novel AND r.state = 'staging') - 1, 0)
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
                 AND j.attempts <= 3 AND j.retry_at IS NOT NULL),
       coalesce(s.evaluation, '{}'::jsonb) <> '{}'::jsonb,
       greatest((SELECT count(*) FROM event_revision r
                  WHERE r.novel_id = novel AND r.state = 'staging') - 1, 0)
  FROM (SELECT repair_subject_revision(novel, 'events') AS id) subject
  LEFT JOIN event_active a ON true
  LEFT JOIN event_staging s ON true
$$;

-- What rollback may actually target. `trusted` is returned because rolling back to an
-- untrusted revision does NOT restore its facts (switch preserves trust deliberately),
-- and an operator picking from a list needs to know that before they pick.
CREATE FUNCTION reader_repair_rollback_targets(novel UUID)
RETURNS TABLE(track TEXT, revision_id UUID, trusted BOOLEAN, created_at TIMESTAMPTZ)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  (SELECT 'graph', r.id, r.trusted, r.created_at FROM graph_revision r
    WHERE r.novel_id = novel AND r.state = 'archived'
    ORDER BY r.created_at DESC LIMIT 10)
  UNION ALL
  (SELECT 'events', r.id, r.trusted, r.created_at FROM event_revision r
    WHERE r.novel_id = novel AND r.state = 'archived'
    ORDER BY r.created_at DESC LIMIT 10)
$$;

ALTER FUNCTION reader_repair_status(UUID) OWNER TO repair_reporter;
ALTER FUNCTION reader_repair_rollback_targets(UUID) OWNER TO repair_reporter;
GRANT EXECUTE ON FUNCTION reader_repair_status(UUID),
                          reader_repair_rollback_targets(UUID) TO rls_reader;

COMMIT;
