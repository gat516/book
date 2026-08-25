-- 0011_novel_provider_config.sql — per-novel LLM provider configuration (PLAN.md Phase
-- N3): a novel's own provider/model/API key, encrypted at the application layer,
-- overriding the process-wide LLM_PROVIDER env var for that novel only. Forward-only.
--
-- Separate table, not a column on `novel`: keeps reader-api's "reads novel with zero new
-- grants" true with no column-level grant complexity, mirroring the glossary/
-- glossary_changelog precedent of separate, audit-adjacent tables.
--
-- Different concept from novel.translation_provider (migration 0006): that column is a
-- SERVED-IDENTITY record (what actually answered, written post-hoc by TranslateStage,
-- immutable once set — the §12 risk #15 protection against caching under the wrong
-- model). This table is a REQUESTED-CONFIGURATION record (the operator's choice,
-- editable). Do not conflate them.
BEGIN;

CREATE TABLE novel_provider_config (
  novel_id       UUID PRIMARY KEY REFERENCES novel(id),
  provider       TEXT NOT NULL,         -- anthropic|deepseek|ollama
  model          TEXT,                  -- optional override; NULL = service default
  base_url       TEXT,                  -- ollama custom host, or deepseek base url override
  api_key_cipher BYTEA,                 -- AES-GCM ciphertext; NULL for ollama (no key needed)
  api_key_nonce  BYTEA,                 -- per-value nonce, never reused
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (provider IN ('anthropic', 'deepseek', 'ollama')),
  CHECK ((api_key_cipher IS NULL) = (api_key_nonce IS NULL))
);

-- Deliberately NO grants here: readable only by roles that also hold
-- PROVIDER_CONFIG_ENCRYPTION_KEY out-of-band (ingest-api's owner connection, pipeline's;
-- askai gets a narrow SELECT in a later migration once it needs to decrypt per-novel).

COMMIT;
