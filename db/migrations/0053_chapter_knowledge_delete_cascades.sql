-- 0053_chapter_knowledge_delete_cascades.sql — preserve migration 0030's one-row purge.
-- Audit rows are durable for the life of a novel, but deleting the novel is explicitly
-- destructive and must still cascade through every derived table.
BEGIN;
ALTER TABLE fact_edit_audit DROP CONSTRAINT fact_edit_audit_novel_id_fkey;
ALTER TABLE fact_edit_audit ADD CONSTRAINT fact_edit_audit_novel_id_fkey
  FOREIGN KEY(novel_id) REFERENCES novel(id) ON DELETE CASCADE;
ALTER TABLE fact_edit_audit DROP CONSTRAINT fact_edit_audit_revision_id_fkey;
ALTER TABLE fact_edit_audit ADD CONSTRAINT fact_edit_audit_revision_id_fkey
  FOREIGN KEY(revision_id) REFERENCES graph_revision(id) ON DELETE CASCADE;
ALTER TABLE fact_edit_audit DROP CONSTRAINT fact_edit_audit_fact_id_fkey;
ALTER TABLE fact_edit_audit ADD CONSTRAINT fact_edit_audit_fact_id_fkey
  FOREIGN KEY(fact_id) REFERENCES fact(id) ON DELETE CASCADE;
ALTER TABLE fact_edit_audit DROP CONSTRAINT fact_edit_audit_successor_fact_id_fkey;
ALTER TABLE fact_edit_audit ADD CONSTRAINT fact_edit_audit_successor_fact_id_fkey
  FOREIGN KEY(successor_fact_id) REFERENCES fact(id) ON DELETE CASCADE;
COMMIT;
