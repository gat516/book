-- 0099_record_review_policy_permissive.sql -- make review decisions readable.
--
-- 0097 accidentally created the gate as AS RESTRICTIVE without a permissive base
-- policy. PostgreSQL combines restrictive policies with AND, so rls_reader saw no
-- decisions at all and rejected rows fell through record_row_not_rejected's NULL=true
-- default. This is the sole scoped permissive policy for the append-only overlay.
BEGIN;

DROP POLICY IF EXISTS record_review_gate ON record_review_decision;
CREATE POLICY record_review_gate ON record_review_decision USING (
  novel_id=reader_novel()
  AND generation_id=(SELECT active_record_generation FROM novel WHERE id=reader_novel())
  AND source_chapter<=reader_chapter()
  AND EXISTS (SELECT 1 FROM record_run r WHERE r.novel_id=record_review_decision.novel_id
    AND r.generation_id=record_review_decision.generation_id
    AND r.chapter_index=record_review_decision.source_chapter AND r.status='published')
);

COMMIT;
