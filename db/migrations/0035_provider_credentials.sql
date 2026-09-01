-- Provider credentials shared by every novel, so a key is entered once rather than
-- re-pasted per book.
--
-- Keys were previously only per-novel (novel_provider_config, migration 0011). That made
-- the common case -- several books on one account -- require pasting the same secret once
-- per book, with no way to rotate it in one place. Credentials move up here; a novel row
-- keeps provider and model (which are genuinely per-book choices) and may still carry its
-- own key as an OVERRIDE for a book billed to a different account.
--
-- Resolution order, implemented in the pipeline and askai: the novel's own key if it has
-- one, else this table's key for that provider. Same for base_url.
--
-- Encryption is unchanged: AES-GCM under INGEST_PROVIDER_CONFIG_KEY, applied by
-- ingest-api. Postgres never sees plaintext, here or in novel_provider_config.
BEGIN;

CREATE TABLE provider_credential (
  provider       TEXT PRIMARY KEY
                 CHECK (provider IN ('anthropic', 'deepseek', 'gemini', 'ollama')),
  base_url       TEXT,
  api_key_cipher BYTEA,
  api_key_nonce  BYTEA,
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- Both halves of the ciphertext or neither, mirroring novel_provider_config's own check.
  CHECK ((api_key_cipher IS NULL) = (api_key_nonce IS NULL))
);

-- askai resolves a novel's provider the same way the pipeline does, so it needs to read
-- the fallback credential too (mirrors 0012's grant on novel_provider_config).
GRANT SELECT ON provider_credential TO rls_reader;

COMMIT;
