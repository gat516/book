-- 0101_reader_enrichment_discarded_grant.sql -- let the reader see the discard flag.
--
-- 0100 added chapter.enrichment_discarded, but rls_reader holds COLUMN-level SELECT on
-- chapter, and a column added after such a grant is not covered by it -- the grant lists
-- columns by name, so a new one is simply absent. Reading it raised permission denied and
-- the whole chapter-knowledge status 500'd.
--
-- That flag is what separates "paused" from "queued". A discarded chapter has no
-- record_run row, so its extraction status derives as 'pending' and the reader is told
-- work is waiting for a worker that will never claim it (§0: a surface must not report
-- knowledge state it cannot support).
BEGIN;

GRANT SELECT (enrichment_discarded) ON chapter TO rls_reader;

COMMIT;
