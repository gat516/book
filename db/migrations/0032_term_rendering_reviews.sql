-- Generalize the pre-translation spelling gate into a rendering review.  The source
-- occurrence remains authority; these fields classify terminology, never identity.
BEGIN;

ALTER TABLE character_name_review
  ADD COLUMN term_role TEXT NOT NULL DEFAULT 'chinese_person'
    CHECK (term_role IN ('chinese_person','foreign_person','personal_title','semantic_term')),
  ADD COLUMN rendering_method TEXT NOT NULL DEFAULT 'pinyin'
    CHECK (rendering_method IN ('pinyin','restored_name','translated_title','semantic_translation'));

-- Preserve already-reviewed constraint semantics. Existing pending rows intentionally
-- remain pinyin until the offline refresh re-evaluates their original evidence.
UPDATE character_name_review r
SET term_role = CASE WHEN g.constraint_class='semantic_term' THEN 'semantic_term'
                     ELSE 'chinese_person' END,
    rendering_method = CASE WHEN g.constraint_class='semantic_term' THEN 'semantic_translation'
                            WHEN EXISTS (SELECT 1 FROM jsonb_array_elements(r.candidates) c
                                         WHERE c->>'method'='restored_name') THEN 'restored_name'
                            WHEN EXISTS (SELECT 1 FROM jsonb_array_elements(r.candidates) c
                                         WHERE c->>'method'='translated_title') THEN 'translated_title'
                            ELSE 'pinyin' END
FROM glossary g
WHERE r.novel_id=g.novel_id AND r.source_term=g.source_term AND r.status='approved';

COMMIT;
