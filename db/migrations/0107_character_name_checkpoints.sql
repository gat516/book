-- §0 / §6.1: durable model work, independent of glossary approval and stage completion.
BEGIN;
CREATE TABLE character_name_checkpoint (
  novel_id uuid NOT NULL REFERENCES novel(id) ON DELETE CASCADE,
  chapter_index int NOT NULL CHECK (chapter_index > 0),
  request_key text NOT NULL,
  source_hash text NOT NULL,
  requested_provider text NOT NULL,
  requested_model text NOT NULL,
  served_provider text NOT NULL,
  served_model text NOT NULL,
  response text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (novel_id, chapter_index, request_key)
);
-- Offline source responses are never exposed through reader credentials (§0).
ALTER TABLE character_name_checkpoint ENABLE ROW LEVEL SECURITY;
ALTER TABLE character_name_checkpoint FORCE ROW LEVEL SECURITY;
CREATE POLICY character_name_checkpoint_writer ON character_name_checkpoint
  FOR ALL TO ingest_writer USING (true) WITH CHECK (true);
GRANT SELECT, INSERT ON character_name_checkpoint TO ingest_writer;
COMMIT;
