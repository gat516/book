-- 0047_repair_progress.sql — let a rebuild say what is wrong and show what it is finding.
--
-- Two gaps found by watching a real rebuild run.
--
-- (A) A rebuild can be completely dead and look merely slow. resume() does its model-pin
--     check and connection setup BEFORE the per-chapter try block, so a failure there --
--     an unreachable endpoint, a model that is not installed, a snapshot whose prose
--     changed -- never reaches record_job_failure. Observed: five minutes of connection
--     failures while the panel showed "0 of 26 done" and an empty failure ledger, with
--     the journal as the only place the truth existed. That is exactly the operator-only
--     diagnosis this whole surface exists to remove.
--
--     These columns are on the REVISION, not on a chapter, deliberately. The cause is not
--     any chapter's fault, and attributing it to one would burn that chapter's three
--     retries on an infrastructure problem -- a flapping tunnel would permanently strand
--     chapter 1.
--
-- (B) While a rebuild runs there is nothing to look at. The review report only exists once
--     every chapter is done, so for hours the only signal is a counter that has not moved.
--     repair_progress exposes what the staging revision has actually published so far.
--     It carries claim values and source quotes, so like repair_preview it is granted to
--     repair_operator alone and never to rls_reader.
BEGIN;

-- The new counts and the progress view read entity and graph_evidence, which
-- repair_reporter was never granted (0042 gave it only novel, fact, the revision and job
-- tables, and chapter_event). Without these, reader_repair_status fails for every caller.
GRANT SELECT ON entity, graph_evidence, graph_completion TO repair_reporter;

ALTER TABLE graph_revision ADD COLUMN blocked_category TEXT;
ALTER TABLE graph_revision ADD COLUMN blocked_at TIMESTAMPTZ;
ALTER TABLE event_revision ADD COLUMN blocked_category TEXT;
ALTER TABLE event_revision ADD COLUMN blocked_at TIMESTAMPTZ;

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
  -- Why the run cannot proceed at all, distinct from any individual chapter failing.
  blocked_category    TEXT,
  blocked_at          TIMESTAMPTZ,
  -- Claims and entities only appear when a WHOLE chapter publishes, so they move at
  -- exactly the same moment chapters_done does. They answer "what has it found", not
  -- "is it alive".
  claims_published    BIGINT,
  entities_created    BIGINT,
  -- One row per completed model call, several per chapter. THIS is the heartbeat: it is
  -- the only counter that moves inside a chapter, which on this hardware is the
  -- difference between "working" and "hung" for ten minutes at a time.
  calls_completed     BIGINT
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
       (SELECT count(*) FROM graph_completion c WHERE c.revision_id = subject.id)
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
       (SELECT count(*) FROM event_completion c WHERE c.revision_id = subject.id)
  FROM (SELECT repair_subject_revision(novel, 'events') AS id) subject
  LEFT JOIN event_active a ON true
  LEFT JOIN event_staging s ON true
$$;

-- Live view of what the rebuild has published. Operator-only: these are unreviewed claims
-- with their source quotes, from chapters anywhere in the book. Same exception, and the
-- same justification, as repair_preview.
CREATE FUNCTION repair_progress(novel UUID)
RETURNS TABLE(
  fact_id       BIGINT,
  entity        TEXT,
  kind          TEXT,
  attribute     TEXT,
  value         TEXT,
  chapter_index INT,
  quote         TEXT
)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT f.id, e.canonical, e.kind, f.attribute, f.value, f.source_chapter, ev.quote
    FROM fact f
    JOIN entity e ON e.id = f.entity_id
    LEFT JOIN graph_evidence ev ON ev.id = f.evidence_id
   WHERE f.revision_id = repair_subject_revision(novel, 'graph')
   ORDER BY f.id DESC
   LIMIT 30
$$;

ALTER FUNCTION reader_repair_status(UUID) OWNER TO repair_reporter;
ALTER FUNCTION repair_progress(UUID) OWNER TO repair_reporter;
REVOKE EXECUTE ON FUNCTION reader_repair_status(UUID), repair_progress(UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION reader_repair_status(UUID) TO rls_reader;
-- Claim values and quotes: operator only, never the reader role askai also uses.
GRANT EXECUTE ON FUNCTION repair_progress(UUID) TO repair_operator;

COMMIT;
