-- 0060_graph_chapter_ceiling.sql -- explicitly grow a trusted graph through a chapter.
--
-- The ceiling itself lives in graph_revision.snapshot so it is frozen and hashed with
-- the other rebuild inputs. This migration only admits the new repair intent. Entity
-- and fact storage remains append-only and chapter-indexed (instructions.md §0.2).
BEGIN;

ALTER TABLE repair_request DROP CONSTRAINT repair_request_action_check;
ALTER TABLE repair_request ADD CONSTRAINT repair_request_action_check
  CHECK (action IN ('prepare','review','activate','rollback','reextract',
                    'reextract_apply','discard','extend'));

ALTER TABLE repair_request ADD CONSTRAINT repair_request_extend_scope
  CHECK (action <> 'extend' OR
         (track = 'graph' AND revision_id IS NULL AND chapter_index IS NULL));

COMMIT;
