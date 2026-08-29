-- Runtime diagnostics are not knowledge claims. Preserve earlier measurements as-is.
ALTER TABLE graph_completion ADD COLUMN runtime_metrics jsonb;
