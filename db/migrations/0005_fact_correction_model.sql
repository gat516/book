-- 0005_fact_correction_model.sql — the correction model for fact (§4).
--
-- WHY: 0001 gave fact only assertion columns; §4 also defines retraction/correction —
-- append-only means a wrong extraction is fixed by a NEW row pointing at the old one,
-- never an UPDATE. Adding this before graph.py's insert_facts() exists (PLAN.md 1.4)
-- avoids rewriting the writer once LLM extraction starts producing corrections in 1.5.
-- Forward-only: we never edit 0001, we add here. Safe because fact is empty (state/resolve
-- stages are still stubs).
BEGIN;

ALTER TABLE fact ADD COLUMN kind TEXT NOT NULL DEFAULT 'assertion';
ALTER TABLE fact ADD CONSTRAINT fact_kind_check
  CHECK (kind IN ('assertion', 'retraction', 'correction'));

-- The fact this row corrects/retracts. NULL for a plain assertion.
ALTER TABLE fact ADD COLUMN supersedes BIGINT REFERENCES fact(id);

-- Supports the §4 deterministic tiebreak when multiple fact rows for the same
-- (entity_id, attribute) are visible under RLS at a given source_chapter: newest
-- knowledge wins, then newest story-time, then highest confidence, then row id.
CREATE INDEX fact_tiebreak_idx ON fact
  (entity_id, attribute, valid_from_chapter DESC, source_chapter DESC, confidence DESC, id DESC);

COMMIT;
