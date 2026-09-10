-- 0081_provider_wait_visibility.sql — durable provider backpressure state (§0, §5.4).
--
-- A provider-directed wait is different from a failed chapter: it must not consume a
-- chapter attempt, and it must be visible while the worker waits. The timestamps and
-- category below contain no provider response body or source text.
BEGIN;

ALTER TABLE graph_revision ADD COLUMN provider_wait_since TIMESTAMPTZ;
ALTER TABLE graph_revision ADD COLUMN provider_wait_retry_at TIMESTAMPTZ;
ALTER TABLE graph_revision ADD COLUMN provider_wait_category TEXT;
ALTER TABLE event_revision ADD COLUMN provider_wait_since TIMESTAMPTZ;
ALTER TABLE event_revision ADD COLUMN provider_wait_retry_at TIMESTAMPTZ;
ALTER TABLE event_revision ADD COLUMN provider_wait_category TEXT;

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
  reviewed            BOOLEAN,
  superseded          BIGINT,
  blocked_category    TEXT,
  blocked_at          TIMESTAMPTZ,
  claims_published    BIGINT,
  entities_created    BIGINT,
  calls_completed     BIGINT,
  current_chapter     INT,
  current_since       TIMESTAMPTZ,
  waiting_since       TIMESTAMPTZ,
  waiting_retry_at     TIMESTAMPTZ,
  waiting_category    TEXT
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
                  WHERE r.novel_id = novel AND r.state = 'staging') - 1, 0),
       (SELECT r.blocked_category FROM graph_revision r WHERE r.id = subject.id),
       (SELECT r.blocked_at FROM graph_revision r WHERE r.id = subject.id),
       (SELECT count(*) FROM fact f WHERE f.revision_id = subject.id),
       (SELECT count(*) FROM entity e WHERE e.revision_id = subject.id),
       (SELECT count(*) FROM graph_completion c WHERE c.revision_id = subject.id),
       (SELECT j.chapter_index FROM graph_job j
         WHERE j.revision_id = subject.id AND j.state = 'processing'
         ORDER BY j.chapter_index LIMIT 1),
       (SELECT j.updated_at FROM graph_job j
         WHERE j.revision_id = subject.id AND j.state = 'processing'
         ORDER BY j.chapter_index LIMIT 1),
       (SELECT r.provider_wait_since FROM graph_revision r WHERE r.id = subject.id),
       (SELECT r.provider_wait_retry_at FROM graph_revision r WHERE r.id = subject.id),
       (SELECT r.provider_wait_category FROM graph_revision r WHERE r.id = subject.id)
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
                  WHERE r.novel_id = novel AND r.state = 'staging') - 1, 0),
       (SELECT r.blocked_category FROM event_revision r WHERE r.id = subject.id),
       (SELECT r.blocked_at FROM event_revision r WHERE r.id = subject.id),
       (SELECT count(*) FROM chapter_event e WHERE e.revision_id = subject.id),
       0::bigint,
       (SELECT count(*) FROM event_completion c WHERE c.revision_id = subject.id),
       (SELECT j.chapter_index FROM event_job j
         WHERE j.revision_id = subject.id AND j.state = 'processing'
         ORDER BY j.chapter_index LIMIT 1),
       (SELECT j.updated_at FROM event_job j
         WHERE j.revision_id = subject.id AND j.state = 'processing'
         ORDER BY j.chapter_index LIMIT 1),
       (SELECT r.provider_wait_since FROM event_revision r WHERE r.id = subject.id),
       (SELECT r.provider_wait_retry_at FROM event_revision r WHERE r.id = subject.id),
       (SELECT r.provider_wait_category FROM event_revision r WHERE r.id = subject.id)
  FROM (SELECT repair_subject_revision(novel, 'events') AS id) subject
  LEFT JOIN event_active a ON true
  LEFT JOIN event_staging s ON true
$$;

ALTER FUNCTION reader_repair_status(UUID) OWNER TO repair_reporter;
REVOKE EXECUTE ON FUNCTION reader_repair_status(UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION reader_repair_status(UUID) TO rls_reader;

COMMIT;
