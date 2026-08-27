-- 0016_glossary_poisoning_guards.sql — structural defences against a weak model writing
-- unusable terminology into permanent state. Forward-only.
--
-- Background. resolve.py's stated principle is "confirm against candidates, never
-- free-generate", and every other decision there honours it: parse_decision refuses any
-- entity_id the model wasn't offered. target_term was the exception — the model invents an
-- arbitrary string with no candidate set and no tripwire, and that string is then locked
-- immutably and enforced against every later translation.
--
-- The observed failure: a small model returned the SAME invented name for six different
-- source terms. Each locked. Every subsequent translation then had to contain that one name
-- six times over, which no correct translation ever would, so the novel could never
-- translate again — under any model, permanently, because glossary rows are immutable.
--
-- Two guards here, both aimed at the write path rather than at model quality:
BEGIN;

-- 1. A target term may be claimed by only one source term per novel.
--
-- The primary key already made source terms unique; the collision that broke things was
-- entirely on the target side, which nothing constrained. Genuine collisions do exist
-- (two characters a translator would both render "Chen"), so this is deliberately a
-- per-novel constraint an operator can resolve by choosing distinct renderings — not a
-- claim that collisions are impossible in principle.
CREATE UNIQUE INDEX glossary_novel_target_key ON glossary (novel_id, target_term);

-- 2. Proposed terms are held here until corroborated, instead of locking on first sight.
--
-- A term is promoted into `glossary` only once the model has independently proposed the
-- same source→target mapping in `proposals` >= threshold distinct chapters. A one-off
-- hallucination is therefore never locked: it sits here, harmless, and is superseded if
-- the model settles on something else. This restores "confirm, don't free-generate" to the
-- one place it was missing — the model must agree with itself across contexts.
--
-- Human-supplied terms (the glossary bootstrap and correction endpoints) bypass this
-- entirely: a person IS the corroboration.
CREATE TABLE glossary_candidate (
  novel_id           UUID NOT NULL REFERENCES novel(id),
  source_term        TEXT NOT NULL,
  target_term        TEXT NOT NULL,
  proposals          INT  NOT NULL DEFAULT 1,
  first_seen_chapter INT  NOT NULL,
  last_seen_chapter  INT  NOT NULL,
  PRIMARY KEY (novel_id, source_term, target_term)
);

-- Counting DISTINCT chapters is what makes "independent" meaningful: re-running the same
-- chapter must not promote a term on its own evidence.
CREATE INDEX glossary_candidate_novel_source ON glossary_candidate (novel_id, source_term);

COMMIT;
