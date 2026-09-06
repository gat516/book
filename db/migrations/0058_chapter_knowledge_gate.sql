-- 0058_chapter_knowledge_gate.sql — report the real chapter-extraction predicate.
--
-- The chapter knowledge workspace has been reading `trusted` to decide whether it is
-- writable, but the gate ingest-api actually enforces before it will run a per-chapter
-- extraction (chapter_knowledge.go's startChapterReextract) is
-- `state='active' AND trusted AND NOT legacy AND <chapter is in the active snapshot>`.
-- `initialize_graph_revision()` (0023) creates every novel's first revision
-- `active+trusted+legacy=true`, so `trusted` alone says "writable" for a book that has
-- never been rebuilt and CANNOT take a per-chapter extraction yet (KnowledgeEngine
-- requires a pinned model a legacy revision does not have). Conversely, `prepare()`
-- flips the OLD revision to `trusted=false` the moment a rebuild starts, so a book stuck
-- mid-rebuild reads "not trusted" and gives no hint that finishing (or discarding, see
-- the `discard` action added alongside this migration) the rebuild is the way out.
--
-- Put the real predicate in the one SQL function both reader-api and askai already call,
-- so Go reports rather than reimplements a gate (CLAUDE.md's load-bearing rule; the same
-- predicate already exists, unreachable, as `reader-api/repair.go`'s `CanReextract`).
BEGIN;

-- CREATE OR REPLACE cannot add OUT columns to an existing function's return row type
-- (Postgres: "cannot change return type of existing function"); the old three-column
-- TABLE(...) must go first.
DROP FUNCTION reader_knowledge_status(int);
CREATE FUNCTION reader_knowledge_status(ch int)
RETURNS TABLE(revision_id uuid,version bigint,trusted boolean,status text,
              legacy boolean,chapter_snapshotted boolean,can_extract boolean)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=public AS $$
 SELECT r.id,r.version,r.trusted,
 CASE WHEN NOT r.trusted THEN 'repair' WHEN r.legacy THEN 'ready' ELSE COALESCE(j.state,'pending') END,
 r.legacy,
 EXISTS(SELECT 1 FROM jsonb_array_elements(r.snapshot->'chapters') c WHERE (c->>'chapter')::int=ch),
 r.trusted AND NOT r.legacy
   AND EXISTS(SELECT 1 FROM jsonb_array_elements(r.snapshot->'chapters') c WHERE (c->>'chapter')::int=ch)
 FROM novel n JOIN graph_revision r ON r.id=n.active_graph_revision
 LEFT JOIN graph_job j ON j.revision_id=r.id AND j.chapter_index=ch
 WHERE n.id=reader_novel() AND ch<=reader_chapter()
$$;
-- Postgres grants EXECUTE to PUBLIC on every new function by default (CLAUDE.md); 0046
-- only revoked it for the functions IT added, so this one has stood PUBLIC-executable
-- since 0023. Close that here while re-creating it, rather than leaving the gap to the
-- next migration that happens to touch this function.
REVOKE ALL ON FUNCTION reader_knowledge_status(int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION reader_knowledge_status(int) TO rls_reader;

-- `discard` (pipeline/graph_rebuild.discard) undoes prepare()'s precautionary quarantine
-- of the revision it superseded — the one escape a stalled rebuild does not currently
-- have. Rollback (already in this list) is a different operation: it targets an archived
-- revision and deliberately preserves distrust, because that revision was distrusted for
-- a reason. Discard targets a still-staging revision's OWN quarantine victim, identified
-- by the graph_audit row prepare wrote, and restores it only if nothing has quarantined
-- it again since (§0: trusted=false stays a human decision; this reverses one specific
-- decision, not a blanket reset).
ALTER TABLE repair_request DROP CONSTRAINT repair_request_action_check;
ALTER TABLE repair_request ADD CONSTRAINT repair_request_action_check
  CHECK (action IN ('prepare','review','activate','rollback','reextract','reextract_apply','discard'));
-- discard operates on the staging revision itself (revision_id), not a chapter, so it
-- stays on the same side of 0052's chapter-scope CHECK as prepare/review/activate/rollback.

COMMIT;
