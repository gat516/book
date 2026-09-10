-- Remove the per-novel API key, leaving provider_credential (migration 0035) as the one
-- place a key is stored.
--
-- 0011 gave novel_provider_config its own key because there was nowhere else to put one.
-- 0035 added account-wide credentials and demoted the per-novel key to an OVERRIDE for
-- "a book billed to a different account" -- a case that never arrived. What did arrive
-- was the cost of keeping it:
--
--   * The key could be written but never cleared. Reads are masked (api_key_set, never
--     the key), so a client cannot round-trip it, so the upsert had to COALESCE the old
--     ciphertext forward. That is one-way: once a book had a key there was no way back to
--     the account key.
--   * A key outlived the provider it was entered for. Switching a book to another provider
--     kept the old ciphertext, and resolution prefers the novel's key over the account's,
--     so the book silently authenticated the new provider with the old provider's secret.
--   * The UI could not describe any of this honestly. api_key_set is a property of the ROW,
--     not of the provider selected, so the panel reported a stale key as "saved for this
--     novel" while hiding the account key that the reader actually wanted.
--
-- The per-book choices that are genuinely per-book -- provider, models, base_url -- all
-- stay. Only the secret moves out.
--
-- This DROP destroys any stored per-novel key. That is the point: the account credential
-- for the same provider remains in provider_credential, and a book that needs a different
-- one is now expressly unsupported rather than half-supported.

ALTER TABLE novel_provider_config
  DROP CONSTRAINT IF EXISTS novel_provider_config_check;

ALTER TABLE novel_provider_config
  DROP COLUMN IF EXISTS api_key_cipher,
  DROP COLUMN IF EXISTS api_key_nonce;
