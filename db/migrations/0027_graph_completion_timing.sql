BEGIN;
ALTER TABLE graph_completion ADD COLUMN elapsed_seconds double precision;
COMMIT;
