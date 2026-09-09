-- 0071_chapter_failure_reader_id_grant.sql — forward correction for 0070 (§0).
--
-- The chapter failure lateral query orders equal timestamps by id. Some databases
-- applied 0070 before id was included in its column grant, so repeat the complete safe
-- metadata grant in a new migration instead of editing applied history.
BEGIN;

GRANT SELECT (id, novel_id, chapter_index, error_code, occurred_at)
  ON chapter_failure TO reader_progress_writer;

COMMIT;
