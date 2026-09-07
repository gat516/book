-- 0059_graph_delete_chapter_run_cascade.sql -- let explicit graph deletion remove its
-- chapter review workspaces. 0052 added this FK after 0030's schema-wide cascade pass,
-- so deleting a revision was still blocked by its derived review rows.
--
-- This applies only to an explicit graph lifecycle deletion. Ordinary fact publication
-- remains append-only and chapter-indexed (instructions.md §0.2).
BEGIN;

ALTER TABLE chapter_knowledge_run
  DROP CONSTRAINT chapter_knowledge_run_revision_id_fkey;
ALTER TABLE chapter_knowledge_run
  ADD CONSTRAINT chapter_knowledge_run_revision_id_fkey
  FOREIGN KEY (revision_id) REFERENCES graph_revision(id) ON DELETE CASCADE;

COMMIT;
