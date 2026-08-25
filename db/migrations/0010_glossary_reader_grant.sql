-- 0010_glossary_reader_grant.sql — lets reader-api's rls_reader connection read the
-- glossary table (PLAN.md Phase N2: GET /novels/{id}/glossary). Forward-only.
--
-- glossary carries no RLS policy (0002_rls.sql enables it only on
-- fact/edge/event/chunk/entity/alias) — the app-layer `locked_at_chapter <= at` filter
-- (reader-api's ListGlossary) is the only gate here, not backed by an RLS inner layer,
-- same posture accepted for chunk before RLS existed. glossary_changelog is
-- deliberately NOT granted: it's an audit trail, not a reader-facing view.
BEGIN;

GRANT SELECT ON glossary TO rls_reader;

COMMIT;
