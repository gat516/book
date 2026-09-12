-- 0094_optional_chunk_embeddings.sql — chunk vectors are a derived retrieval index.
--
-- Translation, records, and display scan remain publishable when an embedding API is
-- unavailable. The chunk text is still durable and can be semantically re-embedded
-- later; a NULL vector simply makes that row ineligible for vector retrieval. Keep the
-- pgvector type/width constraint from 0004, but drop the old NOT NULL gate from 0001.
BEGIN;
ALTER TABLE chunk ALTER COLUMN embedding DROP NOT NULL;
COMMIT;
