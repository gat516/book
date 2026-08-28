-- Reading readiness is independent of optional graph enrichment. A saved, validated
-- translation stays readable even when a subsequent graph stage fails (§0.3, §5).
ALTER TABLE chapter ADD COLUMN translation_ready BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE chapter ADD COLUMN enrichment_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE chapter ADD COLUMN enrichment_retry_at TIMESTAMPTZ;

UPDATE chapter c SET translation_ready = true
WHERE c.status = 'done' OR (
    c.translated_uri IS NOT NULL AND (
        c.translated_by = 'external' OR EXISTS (
            SELECT 1 FROM job j WHERE j.novel_id = c.novel_id
            AND j.chapter_index = c.chapter_index AND j.stage = 'translate' AND j.state = 'done'
        )
    )
);
-- Retry existing enrichment failures after rollout, without hiding their translations.
UPDATE chapter SET enrichment_retry_at = now()
WHERE translation_ready AND status = 'error';
CREATE INDEX chapter_enrichment_retry ON chapter(enrichment_retry_at)
WHERE translation_ready AND status = 'error';
