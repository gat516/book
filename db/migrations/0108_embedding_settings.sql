-- Embeddings are optional derived indexes (§0, §5.4), configured independently of
-- completion. Model provenance prevents comparing vectors from different spaces.
BEGIN;
ALTER TABLE provider_credential DROP CONSTRAINT provider_credential_provider_check;
ALTER TABLE provider_credential ADD CONSTRAINT provider_credential_provider_check
  CHECK (provider IN ('anthropic','custom','deepseek','gemini','groq','ollama','openrouter'));
CREATE TABLE embedding_config (
  singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK (singleton),
  provider TEXT NOT NULL CHECK (provider IN ('auto','disabled','server','gemini','openrouter')),
  model TEXT NOT NULL DEFAULT '',
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
GRANT SELECT ON embedding_config TO rls_reader;
ALTER TABLE chunk ADD COLUMN embedding_space TEXT;
ALTER TABLE entity ADD COLUMN embedding_space TEXT;
GRANT SELECT (embedding_space) ON chunk, entity TO rls_reader;
COMMIT;
