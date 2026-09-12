-- 0096_records_rebuild_discard.sql -- recoverable generation rebuilds.
--
-- A rebuild makes a new generation active before work is queued. Keep its exact
-- predecessor so an operator can abandon that replacement without guessing which
-- generation to restore.
BEGIN;

ALTER TABLE record_generation
  ADD COLUMN predecessor_generation_id UUID;
ALTER TABLE record_generation
  ADD CONSTRAINT record_generation_predecessor_fkey
  FOREIGN KEY (predecessor_generation_id, novel_id)
  REFERENCES record_generation(id, novel_id);

COMMIT;
