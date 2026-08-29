-- A mention can be visible before its identity or facts are known. These remain
-- chapter-gated derived spans (§0.3); NULL never creates or guesses an entity.
BEGIN;
ALTER TABLE mention_span ALTER COLUMN entity_id DROP NOT NULL;
COMMIT;
