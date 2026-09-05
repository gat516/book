-- 0049_repair_proposed_claims.sql — show facts as they are proposed, not after a chapter.
--
-- The panel could show terms live but not facts, because facts are only written to `fact`
-- when a whole chapter publishes. That left the one question that matters to someone
-- watching a rebuild -- "is it actually extracting anything?" -- unanswerable until the
-- end. On the run this was written for, the single completed propose call generated 1501
-- tokens, made 11 identity decisions, and proposed ZERO claims. Nothing in the UI could
-- have told you that.
--
-- Reads the per-call cache, so a claim shows up as soon as the call that proposed it
-- finishes. These are proposals: they have not passed mention resolution, the literal
-- evidence check in graph_write, or review. Some will never be published.
BEGIN;

CREATE FUNCTION repair_proposed_claims(novel UUID)
RETURNS TABLE(kind TEXT, attribute TEXT, value TEXT, quote TEXT, mentions INT)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT c->>'type', c->>'attribute', c->>'value', c->>'quote',
         coalesce(jsonb_array_length(c->'mention_ids'), 0)
    FROM graph_completion g,
         LATERAL jsonb_array_elements(g.response->'claims') c
   WHERE g.revision_id = repair_subject_revision(novel, 'graph')
     AND jsonb_typeof(g.response->'claims') = 'array'
   LIMIT 40
$$;

ALTER FUNCTION repair_proposed_claims(UUID) OWNER TO repair_reporter;
REVOKE EXECUTE ON FUNCTION repair_proposed_claims(UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION repair_proposed_claims(UUID) TO repair_operator;

COMMIT;
