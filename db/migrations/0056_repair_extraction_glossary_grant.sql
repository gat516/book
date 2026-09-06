-- 0056_repair_extraction_glossary_grant.sql — 0055's glossary join runs as repair_reporter
-- (repair_extraction is SECURITY DEFINER), and repair_reporter was never granted glossary:
-- 0042 only gave it novel, fact, the revision/job tables and chapter_event, and nothing
-- since needed more until 0055's join. Without this every call fails permission denied.
BEGIN;

GRANT SELECT ON glossary TO repair_reporter;

COMMIT;
