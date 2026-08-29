-- Novel deletion. Everything in this schema except `novel` itself is derived, novel-scoped
-- data (§0: chapter-indexed facts belong to exactly one novel), so removing a novel means
-- removing its whole subtree. Doing that as an ordered pile of DELETEs in application code
-- would be a second, hand-maintained copy of the FK graph that silently rots every time a
-- migration adds a table — so the database owns the ordering instead: every FK becomes
-- ON DELETE CASCADE and `DELETE FROM novel` is the entire operation.
--
-- This is not a licence to delete elsewhere: append-only discipline (§0.1) is about what
-- the pipeline may do to facts, and nothing in the pipeline deletes. Cascade only ever
-- fires from an explicit operator-initiated novel deletion.
BEGIN;

-- 1. Two tables carry novel_id without a constraint behind it, so cascade could not reach
--    them. Adopt them into the FK graph (clearing any pre-existing orphans first, which by
--    definition point at a novel that no longer exists).
DELETE FROM job                WHERE novel_id NOT IN (SELECT id FROM novel);
DELETE FROM glossary_changelog WHERE novel_id NOT IN (SELECT id FROM novel);

ALTER TABLE job
  ADD CONSTRAINT job_novel_id_fkey FOREIGN KEY (novel_id) REFERENCES novel(id);
ALTER TABLE glossary_changelog
  ADD CONSTRAINT glossary_changelog_novel_id_fkey FOREIGN KEY (novel_id) REFERENCES novel(id);

-- 2. Rewrite every FK to cascade. Done as a loop over pg_constraint rather than ~65 hand
--    written ALTERs: the point is that no edge of the graph is missed, and an enumeration
--    is exactly the thing that can miss one. Every definition in this schema is a plain
--    `FOREIGN KEY (…) REFERENCES …` with no ON UPDATE/MATCH/DEFERRABLE clause, so appending
--    the action to the definition text is a faithful rewrite.
DO $$
DECLARE
  c RECORD;
  def TEXT;
BEGIN
  FOR c IN
    SELECT con.oid, con.conname, con.conrelid::regclass::text AS child
      FROM pg_constraint con
      JOIN pg_class rel ON rel.oid = con.conrelid
     WHERE con.contype = 'f'
       AND rel.relnamespace = 'public'::regnamespace
       -- novel.active_graph_revision points *down* into the subtree it owns; cascading it
       -- would let deleting a revision delete the novel. It is handled below.
       AND con.conname <> 'novel_active_graph_revision_fkey'
  LOOP
    def := pg_get_constraintdef(c.oid);
    IF def LIKE '%ON DELETE%' THEN
      CONTINUE;  -- already has an action (chapter_failure was born cascading)
    END IF;
    EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', c.child, c.conname);
    EXECUTE format('ALTER TABLE %s ADD CONSTRAINT %I %s ON DELETE CASCADE',
                   c.child, c.conname, def);
  END LOOP;
END $$;

-- 3. The one cycle in the graph: novel -> graph_revision -> novel. SET NULL keeps a
--    revision deletion from taking its novel with it; the novel row's own deletion removes
--    the reference before its revisions are reached, so the cycle never blocks.
ALTER TABLE novel DROP CONSTRAINT novel_active_graph_revision_fkey;
ALTER TABLE novel ADD CONSTRAINT novel_active_graph_revision_fkey
  FOREIGN KEY (active_graph_revision) REFERENCES graph_revision(id) ON DELETE SET NULL;

COMMIT;
