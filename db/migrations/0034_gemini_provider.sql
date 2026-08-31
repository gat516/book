-- Allow 'gemini' as a per-novel provider.
--
-- A provider name is enumerated in five places across four languages: this CHECK, the Go
-- validator in ingest-api/handlers.go, build_provider in both the pipeline and askai, and
-- the TypeScript union in the web client. The constraint is the backstop -- it is what
-- stops a row the rest of the stack cannot construct a provider for.
--
-- Gemini reaches us through Google's OpenAI-compatible chat-completions endpoint, so it
-- needs no schema beyond the existing provider/model/base_url/api_key shape.
BEGIN;

ALTER TABLE novel_provider_config
  DROP CONSTRAINT novel_provider_config_provider_check;
ALTER TABLE novel_provider_config
  ADD CONSTRAINT novel_provider_config_provider_check
  CHECK (provider = ANY (ARRAY['anthropic'::text, 'deepseek'::text, 'gemini'::text, 'ollama'::text]));

COMMIT;
