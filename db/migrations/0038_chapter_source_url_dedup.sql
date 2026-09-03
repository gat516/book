-- 0038_chapter_source_url_dedup.sql — fast provenance-keyed chapter deduplication.
--
-- Source URLs are the cheapest reliable identity a scraper has: checking them avoids
-- object-store writes, translation queueing, and any LLM work for a page we already own.
-- Raw SHA-256 remains the second, content-addressed guard for mirrors or moved pages.
BEGIN;

CREATE UNIQUE INDEX chapter_novel_source_url_key
  ON chapter (novel_id, (source_meta->>'source_url'))
  WHERE COALESCE(source_meta->>'source_url', '') <> '';

COMMIT;
