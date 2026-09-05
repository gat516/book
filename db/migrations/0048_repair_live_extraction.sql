-- 0048_repair_live_extraction.sql — show which chapter is being read and what it found.
--
-- "0 of 26 chapters done" is true and useless. A chapter takes many model calls and
-- publishes nothing until all of them succeed, so for tens of minutes the panel could not
-- name the chapter in flight or show a single thing the model had said -- while the
-- answers sat in graph_completion the whole time. On the run this was written for: chapter
-- 1 in progress, nine names already extracted, none of it visible anywhere.
--
-- Two additions:
--   current_chapter  -- which chapter the run is actually reading right now.
--   repair_extraction -- the names the model has proposed, from the per-call cache,
--                       before any of it is published.
--
-- graph_completion has no chapter column (its key is a prompt digest), so extraction is
-- reported per revision rather than per chapter. Only one chapter runs at a time, so in
-- practice this is "what this rebuild has seen so far", which is the useful reading.
BEGIN;

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
  -- The chapter actually being read right now, and since when. "0 of 26" cannot say
  -- whether it is stuck on the first chapter or working through the twentieth.
  current_chapter     INT,
  current_since       TIMESTAMPTZ
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
         ORDER BY j.chapter_index LIMIT 1)
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
         ORDER BY j.chapter_index LIMIT 1)
  FROM (SELECT repair_subject_revision(novel, 'events') AS id) subject
  LEFT JOIN event_active a ON true
  LEFT JOIN event_staging s ON true
$$;

-- What the model has proposed but not yet published. Operator-only, like every other view
-- of unreviewed material: these are surfaces and their source quotes, and nothing here has
-- passed the checks that decide whether a name becomes an entity.
CREATE FUNCTION repair_extraction(novel UUID)
RETURNS TABLE(surface TEXT, kind TEXT, named BOOLEAN, quote TEXT)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT DISTINCT ON (n->>'surface')
         n->>'surface', n->>'kind', (n->>'named')::boolean, n->>'quote'
    FROM graph_completion c,
         LATERAL jsonb_array_elements(c.response->'names') n
   WHERE c.revision_id = repair_subject_revision(novel, 'graph')
     AND jsonb_typeof(c.response->'names') = 'array'
   ORDER BY n->>'surface'
   LIMIT 60
$$;

ALTER FUNCTION reader_repair_status(UUID) OWNER TO repair_reporter;
ALTER FUNCTION repair_extraction(UUID) OWNER TO repair_reporter;
REVOKE EXECUTE ON FUNCTION reader_repair_status(UUID), repair_extraction(UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION reader_repair_status(UUID) TO rls_reader;
GRANT EXECUTE ON FUNCTION repair_extraction(UUID) TO repair_operator;

COMMIT;
