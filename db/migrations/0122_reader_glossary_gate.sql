-- §0.3: glossary rendering must compose chapter clearance with ownership.
BEGIN;
CREATE POLICY glossary_knowledge_boundary ON glossary AS RESTRICTIVE FOR SELECT TO rls_reader
 USING(novel_id=reader_novel() AND locked_at_chapter<=reader_chapter());
COMMIT;
