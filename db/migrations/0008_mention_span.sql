-- 0008_mention_span.sql — display spans for the reader UI's mention highlighting
-- (instructions.md §5 step 6; PLAN.md Phase 5.2). Forward-only.
--
-- Derived data, not knowledge: delete-and-reinsert per chapter, same discipline as
-- `chunk` (0001), not the append-only discipline `fact`/`edge` use (§0.2 doesn't apply
-- to derived data). RLS mirrors `gate_chunk` (0002) for the same defense-in-depth
-- consistency, even though the reader-api chapter endpoint's own `n <= progress` check
-- is independently sufficient — every other per-chapter-derived table gets it too.
BEGIN;

CREATE TABLE mention_span (
  id            BIGSERIAL PRIMARY KEY,
  novel_id      UUID NOT NULL REFERENCES novel(id),
  chapter_index INT  NOT NULL,
  entity_id     UUID NOT NULL REFERENCES entity(id),
  char_start    INT  NOT NULL,
  char_end      INT  NOT NULL
);
CREATE INDEX ON mention_span (novel_id, chapter_index);

ALTER TABLE mention_span ENABLE ROW LEVEL SECURITY;
ALTER TABLE mention_span FORCE ROW LEVEL SECURITY;
CREATE POLICY gate_mention_span ON mention_span
  USING (novel_id = reader_novel() AND chapter_index <= reader_chapter());

GRANT SELECT ON mention_span TO rls_reader;

COMMIT;
