-- 0055_repair_extraction_gloss.sql — show what a proposed term is already known as in
-- English, while it is still just a surface nothing has resolved yet.
--
-- repair_extraction (0048) returns the surface exactly as the model wrote it -- source
-- language, because that is all that exists at this point: RESOLVE has not run, so there
-- is no entity and therefore no canonical name to prefer. But a name is not always new.
-- glossary is populated across the whole book already (novel_provider config aside, this
-- is the same source_term -> target_term ledger resolve.py maintains and the web
-- Glossary panel edits), so a surface the model re-proposes on a rebuild very often
-- already has a locked target_term sitting right there. Join it in, nullable: a brand
-- new name has no gloss yet, and that is honest -- it means nobody has decided its
-- English rendering, not that this query is broken.
--
-- Deliberately the glossary table, not a model-produced gloss like fact.value_en (0051).
-- A name's English form is not the model's to invent -- it is the one already locked, or
-- none. Asking the model to also guess an English spelling for names would just create a
-- second, competing answer to a question glossary already owns.
BEGIN;

DROP FUNCTION repair_extraction(UUID);

CREATE FUNCTION repair_extraction(novel UUID)
RETURNS TABLE(surface TEXT, kind TEXT, named BOOLEAN, quote TEXT, target_term TEXT)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT DISTINCT ON (n->>'surface')
         n->>'surface', n->>'kind', (n->>'named')::boolean, n->>'quote', g.target_term
    FROM graph_completion c,
         LATERAL jsonb_array_elements(c.response->'names') n
    LEFT JOIN glossary g
           ON g.novel_id = novel AND g.source_term = n->>'surface' AND NOT g.deleted
   WHERE c.revision_id = repair_subject_revision(novel, 'graph')
     AND jsonb_typeof(c.response->'names') = 'array'
   ORDER BY n->>'surface'
   LIMIT 60
$$;

-- CREATE OR REPLACE preserves owner and grants; re-asserted so this file stands alone.
ALTER FUNCTION repair_extraction(UUID) OWNER TO repair_reporter;
REVOKE EXECUTE ON FUNCTION repair_extraction(UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION repair_extraction(UUID) TO repair_operator;

COMMIT;
