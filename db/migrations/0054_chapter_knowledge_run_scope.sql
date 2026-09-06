-- 0054_chapter_knowledge_run_scope.sql -- independently request term or fact extraction.
-- A scoped run keeps its own preview and activity stream; §0 publication remains
-- append-only and an extraction never silently expands into the other knowledge kind.
BEGIN;

ALTER TABLE chapter_knowledge_run ADD COLUMN scope TEXT NOT NULL DEFAULT 'all'
  CHECK (scope IN ('all','terms','facts'));

DROP INDEX chapter_knowledge_one_live_reextract;
CREATE UNIQUE INDEX chapter_knowledge_one_live_reextract_scope
  ON chapter_knowledge_run(novel_id,chapter_index,scope)
  WHERE mode='reextract' AND state IN ('pending','processing','awaiting_review','applying');

COMMIT;
