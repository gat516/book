-- 0105_fact_first_status.sql -- spoiler-safe progress projection.
-- The reader tables remain published-only. This aggregate is the sole exception for
-- progress reporting and deliberately returns no source, prompt, or raw diagnostics.
BEGIN;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='fact_first_status_owner') THEN
    CREATE ROLE fact_first_status_owner NOLOGIN;
  END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO fact_first_status_owner;
GRANT SELECT ON novel, fact_first_run, fact_first_candidate, fact_first_selection,
  fact_first_assertion, fact_first_fact, fact_first_relation, fact_first_event
  TO fact_first_status_owner;
GRANT EXECUTE ON FUNCTION reader_novel(), reader_chapter() TO fact_first_status_owner;

CREATE POLICY fact_first_run_status_owner ON fact_first_run TO fact_first_status_owner USING (
  novel_id=reader_novel() AND generation_id=(SELECT active_record_generation FROM novel WHERE id=reader_novel())
  AND chapter_index<=reader_chapter());
DO $$ DECLARE t TEXT; BEGIN
  FOREACH t IN ARRAY ARRAY['fact_first_candidate','fact_first_selection','fact_first_assertion','fact_first_fact','fact_first_relation','fact_first_event'] LOOP
    EXECUTE format('CREATE POLICY %I_status_owner ON %I TO fact_first_status_owner USING (EXISTS (SELECT 1 FROM fact_first_run r WHERE r.id=run_id AND r.novel_id=reader_novel() AND r.generation_id=(SELECT active_record_generation FROM novel WHERE id=reader_novel()) AND r.chapter_index<=reader_chapter()))', t, t);
  END LOOP;
END $$;

CREATE FUNCTION reader_fact_first_status(p_novel UUID, p_generation UUID, p_chapter INT)
RETURNS JSONB
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
WITH allowed AS (
  SELECT r.*
    FROM fact_first_run r
    JOIN novel n ON n.id=r.novel_id AND n.active_record_generation=r.generation_id
   WHERE p_novel=reader_novel() AND r.novel_id=p_novel AND r.generation_id=p_generation
     AND r.chapter_index<=reader_chapter()
     AND (p_chapter IS NULL OR r.chapter_index=p_chapter)
), rejected AS (
  SELECT count(DISTINCT (a.id, x.value->'row'->>'assertion_id')) FILTER
           (WHERE x.value->'row'->>'assertion_id' IS NOT NULL) AS n
    FROM allowed a
    CROSS JOIN LATERAL jsonb_array_elements(COALESCE(a.diagnostics->'normalization'->'rejected','[]'::jsonb)) x(value)
), stages AS (
  SELECT key, (array_agg(value ORDER BY CASE value
    WHEN 'failed' THEN 4 WHEN 'processing' THEN 3 WHEN 'pending' THEN 2
    WHEN 'ready' THEN 1 WHEN 'completed' THEN 0 ELSE 2 END DESC))[1] AS value
    FROM allowed a
    CROSS JOIN LATERAL jsonb_each_text(COALESCE(a.diagnostics->'stages','{}'::jsonb)) s(key,value)
   GROUP BY key
), counts AS (
  SELECT
    (SELECT count(*) FROM fact_first_candidate c JOIN allowed a ON a.id=c.run_id) discovered,
    (SELECT count(*) FROM fact_first_selection s JOIN allowed a ON a.id=s.run_id WHERE s.decision='keep') selected,
    (SELECT count(*) FROM fact_first_selection s JOIN allowed a ON a.id=s.run_id WHERE s.decision='omit') omitted,
    (SELECT count(*) FROM fact_first_selection s JOIN allowed a ON a.id=s.run_id WHERE s.decision='consolidate') consolidated,
    (SELECT n FROM rejected) rejected,
    (SELECT count(*) FROM fact_first_assertion x JOIN allowed a ON a.id=x.run_id WHERE x.status='lost') unrepresented,
    (SELECT count(*) FROM (SELECT run_id,assertion_id FROM fact_first_fact UNION ALL SELECT run_id,assertion_id FROM fact_first_relation UNION ALL SELECT run_id,assertion_id FROM fact_first_event) x JOIN allowed a ON a.id=x.run_id) published
)
SELECT jsonb_build_object(
  'discovered', discovered, 'selected', selected, 'omitted', omitted,
  'consolidated', consolidated, 'rejected', rejected,
  'unrepresented', unrepresented, 'published', published,
  'stages', COALESCE((SELECT jsonb_object_agg(key,value) FROM stages), '{}'::jsonb)
) FROM counts;
$$;

ALTER FUNCTION reader_fact_first_status(UUID, UUID, INT) OWNER TO fact_first_status_owner;

REVOKE ALL ON FUNCTION reader_fact_first_status(UUID, UUID, INT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION reader_fact_first_status(UUID, UUID, INT) TO rls_reader;
COMMIT;
