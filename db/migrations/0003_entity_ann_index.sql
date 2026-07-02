-- 0003_entity_ann_index.sql — ivfflat ANN index on entity.embedding (§4).
--
-- Run this AFTER seed data exists: ivfflat trains its lists on the rows present at
-- CREATE INDEX time, so building it on an empty table yields a poor index. Re-run
-- (DROP + CREATE) after a large backfill if recall degrades.
--
-- ANN is acceptable HERE (unlike chunk): resolution retrieval is a fuzzy candidate
-- search, not a security boundary, and a missed candidate is caught by exact alias
-- match in the same step.
BEGIN;

CREATE INDEX IF NOT EXISTS entity_embedding_ivfflat
  ON entity USING ivfflat (embedding vector_cosine_ops);

COMMIT;
