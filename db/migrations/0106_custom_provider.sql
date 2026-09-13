-- 0106_custom_provider.sql — custom OpenAI-compatible APIs remain behind the LLMProvider
-- seam (§5.4) and carry their own durable provider identity (§14.3).
BEGIN;

ALTER TABLE novel_provider_config DROP CONSTRAINT novel_provider_config_provider_check;
ALTER TABLE novel_provider_config ADD CONSTRAINT novel_provider_config_provider_check
  CHECK (provider = ANY (ARRAY['anthropic','custom','deepseek','gemini','groq','ollama']));

ALTER TABLE provider_credential DROP CONSTRAINT provider_credential_provider_check;
ALTER TABLE provider_credential ADD CONSTRAINT provider_credential_provider_check
  CHECK (provider = ANY (ARRAY['anthropic','custom','deepseek','gemini','groq','ollama']));

COMMIT;
