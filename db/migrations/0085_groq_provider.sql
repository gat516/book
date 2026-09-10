-- 0085_groq_provider.sql — add Groq at the existing LLMProvider seam (§5.4).
BEGIN;

ALTER TABLE novel_provider_config DROP CONSTRAINT novel_provider_config_provider_check;
ALTER TABLE novel_provider_config ADD CONSTRAINT novel_provider_config_provider_check
  CHECK (provider = ANY (ARRAY['anthropic','deepseek','gemini','groq','ollama']));

ALTER TABLE provider_credential DROP CONSTRAINT provider_credential_provider_check;
ALTER TABLE provider_credential ADD CONSTRAINT provider_credential_provider_check
  CHECK (provider = ANY (ARRAY['anthropic','deepseek','gemini','groq','ollama']));

ALTER TABLE graph_completion DROP CONSTRAINT graph_completion_served_provider_check;
ALTER TABLE graph_completion ADD CONSTRAINT graph_completion_served_provider_check
  CHECK (served_provider = ANY (ARRAY['anthropic','deepseek','gemini','groq','ollama']));

ALTER TABLE event_completion DROP CONSTRAINT event_completion_served_provider_check;
ALTER TABLE event_completion ADD CONSTRAINT event_completion_served_provider_check
  CHECK (served_provider = ANY (ARRAY['anthropic','deepseek','gemini','groq','ollama']));

COMMIT;
