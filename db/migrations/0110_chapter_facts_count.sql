-- 0110_chapter_facts_count.sql -- mark a chapter's enrichment done without exposing facts.
--
-- FACTS (0109) replaced RECORDS, but every "is this chapter done?" check (reader status,
-- book coverage, retry/extract/stop) still looked for a published record_run, which the
-- runtime no longer writes. Every chapter therefore read as waiting forever.
--
-- FACTS is the last enrichment stage and writes nothing when it gets no usable facts, so
-- "this chapter has facts" is the new done marker. It lives on chapter rather than behind
-- a reader grant on chapter_fact: 0109 keeps fact TEXT away from readers until the wiki
-- step defines a read path, and a count reveals only that the stage ran for a chapter the
-- reader is already gated to (§0).
BEGIN;

ALTER TABLE chapter ADD COLUMN facts_count INTEGER CHECK (facts_count >= 0);

-- Chapters processed before this migration: the newest prompt version's set counts.
UPDATE chapter c SET facts_count = f.n
  FROM (SELECT DISTINCT ON (novel_id, chapter_index) novel_id, chapter_index, count(*) AS n
          FROM chapter_fact
         GROUP BY novel_id, chapter_index, prompt_version
         ORDER BY novel_id, chapter_index, prompt_version DESC) f
 WHERE c.novel_id = f.novel_id AND c.chapter_index = f.chapter_index;

-- rls_reader holds column-level SELECT on chapter; a new column is not covered (see 0101).
GRANT SELECT (facts_count) ON chapter TO rls_reader;

COMMIT;
