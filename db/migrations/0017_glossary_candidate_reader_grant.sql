-- 0017_glossary_candidate_reader_grant.sql — let reader-api read the provisional
-- terminology table so it can report translation stability to the reader. Forward-only.
--
-- glossary_candidate (0016) records every target the model has proposed for a source term.
-- A source that has accumulated several DIFFERENT proposed targets is direct evidence the
-- model is not naming that entity consistently — which is exactly what a reader needs
-- warning about, and is measurable without judging translation quality itself.
--
-- Same posture as 0010's grant on `glossary`: SELECT only, to the role reader-api reads
-- with. Nothing here is more sensitive than the locked glossary it sits beside.
BEGIN;

GRANT SELECT ON glossary_candidate TO rls_reader;

COMMIT;
