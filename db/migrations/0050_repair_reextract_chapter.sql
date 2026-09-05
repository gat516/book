-- 0050_repair_reextract_chapter.sql — a chapter-scoped repair action.
--
-- Re-extracting one chapter and rebuilding a novel are different operations with wildly
-- different costs, and the UI could only offer the second. Someone inside chapter 4 asking
-- to redo chapter 4 was shown a warning about re-extracting all 26.
--
-- The machinery already exists: enqueue_completed appends a chapter to the live graph and
-- drain_active extracts exactly one chapter per idle tick. What was missing is a way to
-- ASK for one chapter, so this adds the action and the chapter it applies to.
--
-- It only ever targets the active, trusted, managed revision -- the graph readers are
-- actually served. A quarantined or legacy graph has no trusted revision to append to,
-- which is what quarantine means; the executor refuses rather than silently doing nothing.
BEGIN;

ALTER TABLE repair_request ADD COLUMN chapter_index INT;

ALTER TABLE repair_request DROP CONSTRAINT repair_request_action_check;
ALTER TABLE repair_request ADD CONSTRAINT repair_request_action_check
  CHECK (action IN ('prepare', 'review', 'activate', 'rollback', 'reextract'));

-- A chapter-scoped request must name a chapter; a whole-graph one must not.
ALTER TABLE repair_request ADD CONSTRAINT repair_request_chapter_scope
  CHECK ((action = 'reextract') = (chapter_index IS NOT NULL));

COMMIT;
