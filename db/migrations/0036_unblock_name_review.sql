-- Release chapters parked at needs_name_review.
--
-- The character-name gate used to hold a chapter unread until a human approved every
-- proposed rendering -- in practice 15+ approvals for a single chapter. What §0 protects
-- is the glossary, not the translation: a locked term is immutable and primed into every
-- later chapter, so a bad lock is unrecoverable, while translating with an unlocked,
-- model-chosen rendering risks nothing permanent. The proposals are still recorded and
-- still require approval (or GLOSSARY_MIN_PROPOSALS agreement) before they lock; the
-- reader now corrects a name by clicking it in the prose instead of before reading it.
--
-- Nothing writes this status any more, so any row still holding it is stranded the same
-- way pre-TRANSLATE failures were before 0033: the retry sweep could not see it.
BEGIN;

-- Match the widened sweep so these rows are actually indexed for it.
DROP INDEX IF EXISTS chapter_enrichment_retry;
CREATE INDEX chapter_enrichment_retry ON chapter(enrichment_retry_at)
WHERE status IN ('error', 'name_repair_error', 'needs_name_review');

UPDATE chapter SET enrichment_retry_at = now(), enrichment_attempts = 0
WHERE status = 'needs_name_review';

COMMIT;
