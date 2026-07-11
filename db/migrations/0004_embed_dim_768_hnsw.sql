-- 0004_embed_dim_768_hnsw.sql — correct the embedding dimension and switch the entity
-- ANN index from ivfflat to HNSW (research review §1.1 blocker + §4.4).
--
-- WHY (§1.1, HARD BLOCKER): entity/chunk.embedding were VECTOR(1024) but the stated model
-- is nomic-embed-text, which outputs 768 dims. pgvector does NOT coerce — inserting a
-- 768-vec into a 1024 column throws "expected 1024 dimensions, not 768". EMBED_DIM (§10),
-- the model output, and BOTH columns must agree on 768. Forward-only: we never edit 0001,
-- we correct here. Safe because no embeddings exist yet (pipeline unbuilt).
--
-- WHY HNSW (§4.4): ivfflat trains lists on rows present at CREATE time, so it is
-- useless-to-harmful on the tiny/early entity table and drifts under continuous inserts
-- (needs scheduled REINDEX). HNSW builds on an empty table and handles streaming inserts —
-- exactly this workload. This retires the "build only after seed data exists" dance in 0003.
BEGIN;

-- Dimension: 1024 -> 768. Tables are empty, so the type change is a metadata-only rewrite.
ALTER TABLE entity ALTER COLUMN embedding TYPE VECTOR(768);
ALTER TABLE chunk  ALTER COLUMN embedding TYPE VECTOR(768);

-- Swap ivfflat (0003) for HNSW on the entity ANN index.
DROP INDEX IF EXISTS entity_embedding_ivfflat;
CREATE INDEX IF NOT EXISTS entity_embedding_hnsw
  ON entity USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 200);

-- chunk deliberately has NO ANN index (security boundary — exact scan only, §4).

-- RLS performance fix (§4.2 of the review, §13.1): 0002 called the STABLE helpers bare in
-- USING(), which Postgres evaluates as a per-row SubPlan (→ seq scan). Wrap each call in a
-- scalar subquery so it becomes a once-per-query InitPlan. Correctness is unchanged; this is
-- purely a plan fix. Forward-only: recreate the policies rather than editing 0002.
DROP POLICY IF EXISTS gate_fact   ON fact;
DROP POLICY IF EXISTS gate_edge   ON edge;
DROP POLICY IF EXISTS gate_event  ON event;
DROP POLICY IF EXISTS gate_chunk  ON chunk;
DROP POLICY IF EXISTS gate_entity ON entity;
DROP POLICY IF EXISTS gate_alias  ON alias;

CREATE POLICY gate_fact  ON fact
  USING (novel_id = (SELECT reader_novel()) AND source_chapter     <= (SELECT reader_chapter()));
CREATE POLICY gate_edge  ON edge
  USING (novel_id = (SELECT reader_novel()) AND source_chapter     <= (SELECT reader_chapter()));
CREATE POLICY gate_event ON event
  USING (novel_id = (SELECT reader_novel()) AND chapter_index      <= (SELECT reader_chapter()));
CREATE POLICY gate_chunk ON chunk
  USING (novel_id = (SELECT reader_novel()) AND chapter_index      <= (SELECT reader_chapter()));
CREATE POLICY gate_entity ON entity
  USING (novel_id = (SELECT reader_novel()) AND first_seen_chapter <= (SELECT reader_chapter()));
CREATE POLICY gate_alias ON alias
  USING (first_seen_chapter <= (SELECT reader_chapter())
         AND entity_id IN (SELECT id FROM entity));

COMMIT;
