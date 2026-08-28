-- Operational failure history; no model output, credentials, or story text.
CREATE TABLE chapter_failure (
    id BIGSERIAL PRIMARY KEY,
    novel_id UUID NOT NULL REFERENCES novel(id) ON DELETE CASCADE,
    chapter_index INTEGER NOT NULL,
    stage TEXT NOT NULL,
    error_type TEXT NOT NULL,
    error_code TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX chapter_failure_lookup ON chapter_failure(novel_id, chapter_index, occurred_at DESC);
