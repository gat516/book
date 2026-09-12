-- 0102_reader_translation_ready_grant.sql -- let the reader see which chapters are readable.
--
-- The single-chapter knowledge status now reports waiting_on_chapter: the earliest earlier
-- chapter that is readable but not yet extracted. Chapters publish in order -- the worker's
-- generation fence (records_generation.py) refuses a chapter while an earlier
-- translation_ready chapter is unpublished -- so without this the UI offered a Retry that
-- could only fail. rls_reader holds column-level SELECT on chapter, so the column must be
-- granted by name (same trap as 0101).
--
-- Not a spoiler surface (§0): the status query only ever asks about chapters BEFORE the
-- one being read, and readiness says nothing about story content.
BEGIN;

GRANT SELECT (translation_ready) ON chapter TO rls_reader;

COMMIT;
