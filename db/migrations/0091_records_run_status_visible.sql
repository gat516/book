-- 0091_records_run_status_visible.sql -- let a reader see that work is in flight.
--
-- 0089 gated record_run on status='published', which hid every unpublished run from the
-- reader role.  That made the whole progress surface dead: recordsStatusFor could never
-- report 'processing' or 'failed', only 'pending', so a chapter being extracted looked
-- identical to one nobody had started, and the failure detail branch was unreachable.
--
-- A run row is operational metadata (chapter, status, model, warning count, a bounded
-- failure category from pipeline/failures.py) and carries no story text, so it is safe
-- at the reader's own chapter bound.  Content stays gated exactly as before: every child
-- policy in 0089 independently requires EXISTS(... record_run.status='published'), so
-- widening this policy exposes progress, never rows (§0 -- the gate is knowledge-time,
-- and an unpublished run still yields nothing to read).
BEGIN;

DROP POLICY IF EXISTS record_run_gate ON record_run;
CREATE POLICY record_run_gate ON record_run USING (
  novel_id=reader_novel() AND generation_id=(SELECT active_record_generation FROM novel WHERE id=reader_novel())
  AND chapter_index<=reader_chapter()
);

COMMIT;
