-- 0100_chapter_enrichment_discard.sql -- explicit operator discard for one graph attempt.
-- This is a pause, not deletion: published record generations remain immutable (§0).
BEGIN;

ALTER TABLE chapter
  ADD COLUMN IF NOT EXISTS enrichment_discarded BOOLEAN NOT NULL DEFAULT false;

COMMIT;
