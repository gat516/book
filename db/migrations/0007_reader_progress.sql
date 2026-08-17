-- 0007_reader_progress.sql — server-authoritative reader clearance and least-privilege
-- reader-api roles (PLAN.md Phase 2).
BEGIN;

CREATE TABLE reader_progress (
  reader_id       TEXT NOT NULL CHECK (length(btrim(reader_id)) BETWEEN 1 AND 200),
  novel_id        UUID NOT NULL,
  current_chapter INT NOT NULL CHECK (current_chapter >= 0),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (reader_id, novel_id),
  FOREIGN KEY (novel_id, current_chapter)
    REFERENCES chapter (novel_id, chapter_index)
);

-- Roles are cluster-wide while the migration ledger is database-local, so tolerate a
-- role provisioned by another database or deployment bootstrap.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'reader_progress_writer') THEN
    CREATE ROLE reader_progress_writer NOLOGIN;
  END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO rls_reader, reader_progress_writer;
GRANT SELECT ON novel, entity, alias, fact, edge, event, chunk TO rls_reader;
GRANT EXECUTE ON FUNCTION reader_chapter(), reader_novel() TO rls_reader;

GRANT SELECT ON novel, chapter, reader_progress TO reader_progress_writer;
GRANT INSERT, UPDATE ON reader_progress TO reader_progress_writer;

COMMIT;
