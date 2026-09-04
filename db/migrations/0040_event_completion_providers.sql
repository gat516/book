-- Allow event extraction to be served by a provider other than local Ollama.
--
-- Migration 0039 pinned event_completion.served_provider to 'ollama' because event
-- extraction was deliberately local-only: a locally installed model has a digest, so an
-- event revision could name the exact weights that produced every row, and the drift
-- check in event_rebuild could refuse to resume when those weights changed.
--
-- That guarantee cost more than it bought. The pilot (docs/event-extraction-pilot-metrics.md)
-- ran the local 7B to a wall: recall stalled at ~0.50 on chapter 1 across five prompt
-- versions, and the hardware cannot hold a larger model. A hosted model is the only way
-- to test whether the ceiling is the prompt or the parameter count.
--
-- What replaces the digest guarantee is weaker, and deliberately so: a remote model has
-- no digest we can pin, and a vendor may change the weights behind a stable model name.
-- The protections that remain are (a) served_model is recorded on every cached completion
-- and is part of this table's primary key, so a silent rename never reuses another
-- model's cache, (b) EventEngine rejects any completion whose served provider/model is
-- not the one the revision pinned, and (c) nothing reaches a reader without the human
-- review gate, which is unchanged. Reproducibility of a *finished* revision is therefore
-- still auditable; bit-exact *re-derivation* is not, for hosted providers.
--
-- The allowed set mirrors novel_provider_config's own provider CHECK (migrations 0011,
-- 0034) so a provider is enumerated the same way everywhere.
BEGIN;

ALTER TABLE event_completion
  DROP CONSTRAINT event_completion_served_provider_check;
ALTER TABLE event_completion
  ADD CONSTRAINT event_completion_served_provider_check
  CHECK (served_provider = ANY (ARRAY['anthropic'::text, 'deepseek'::text, 'gemini'::text, 'ollama'::text]));

COMMIT;
