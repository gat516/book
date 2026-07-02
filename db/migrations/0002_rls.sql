-- 0002_rls.sql — defense-in-depth chapter gate via Row-Level Security (§13.1).
-- The DB itself refuses to return rows above the reader's progress, so a single
-- app-layer bug cannot leak future chapters. Gates on source_chapter (knowledge-time),
-- NEVER valid_from_chapter (story-time) — see the flashback-revelation note in §4.
BEGIN;

-- Helper: fail CLOSED if the GUC was never set. current_setting on an unset GUC throws;
-- the (…, true) form returns NULL, and COALESCE(-1) then matches nothing.
CREATE FUNCTION reader_chapter() RETURNS int LANGUAGE sql STABLE AS
  $$ SELECT COALESCE(NULLIF(current_setting('app.current_chapter', true), '')::int, -1) $$;
CREATE FUNCTION reader_novel() RETURNS uuid LANGUAGE sql STABLE AS
  $$ SELECT NULLIF(current_setting('app.novel_id', true), '')::uuid $$;

ALTER TABLE fact   ENABLE ROW LEVEL SECURITY;
ALTER TABLE edge   ENABLE ROW LEVEL SECURITY;
ALTER TABLE event  ENABLE ROW LEVEL SECURITY;
ALTER TABLE chunk  ENABLE ROW LEVEL SECURITY;
ALTER TABLE entity ENABLE ROW LEVEL SECURITY;
ALTER TABLE alias  ENABLE ROW LEVEL SECURITY;
-- Owners bypass RLS by default — FORCE closes that hole if the app user owns the tables.
ALTER TABLE fact   FORCE ROW LEVEL SECURITY;
ALTER TABLE edge   FORCE ROW LEVEL SECURITY;
ALTER TABLE event  FORCE ROW LEVEL SECURITY;
ALTER TABLE chunk  FORCE ROW LEVEL SECURITY;
ALTER TABLE entity FORCE ROW LEVEL SECURITY;
ALTER TABLE alias  FORCE ROW LEVEL SECURITY;

CREATE POLICY gate_fact  ON fact
  USING (novel_id = reader_novel() AND source_chapter     <= reader_chapter());
CREATE POLICY gate_edge  ON edge
  USING (novel_id = reader_novel() AND source_chapter     <= reader_chapter());
CREATE POLICY gate_event ON event
  USING (novel_id = reader_novel() AND chapter_index      <= reader_chapter());
CREATE POLICY gate_chunk ON chunk
  USING (novel_id = reader_novel() AND chapter_index      <= reader_chapter());
CREATE POLICY gate_entity ON entity
  USING (novel_id = reader_novel() AND first_seen_chapter <= reader_chapter());
CREATE POLICY gate_alias ON alias
  USING (first_seen_chapter <= reader_chapter()
         AND entity_id IN (SELECT id FROM entity));  -- novel scope via gated entity

-- Two roles: readers are gated, the ingestion writer bypasses RLS (writes ALL chapters).
-- reader-api and askai connect as rls_reader; pipeline connects as ingest_writer.
CREATE ROLE rls_reader    NOLOGIN;
CREATE ROLE ingest_writer NOLOGIN BYPASSRLS;

COMMIT;
