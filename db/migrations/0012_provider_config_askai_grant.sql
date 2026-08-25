-- 0012_provider_config_askai_grant.sql — lets askai's connection read
-- novel_provider_config (PLAN.md Phase N4: per-novel provider selection in Service.ask).
-- Forward-only.
--
-- askai currently connects as rls_reader (the same role reader-api uses — see
-- askai/app.py's _configure_connection) rather than a role of its own; this grant is
-- therefore slightly wider than ideal, since reader-api's connection can now also SELECT
-- the ciphertext columns (harmless without the out-of-band PROVIDER_CONFIG_ENCRYPTION_KEY
-- held only by ingest-api/pipeline/askai processes, but not clean separation). Accepted
-- for now per PLAN.md Phase N4 step 6 — noted there as follow-up debt: give askai its own
-- askai_reader role instead of sharing rls_reader with reader-api.
--
-- No RLS policy needed here (same posture as glossary, migration 0010): the app-layer
-- WHERE novel_id = $1 in every reader of this table is the only gate.
BEGIN;

GRANT SELECT ON novel_provider_config TO rls_reader;

COMMIT;
