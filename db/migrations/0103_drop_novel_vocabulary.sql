-- 0103_drop_novel_vocabulary.sql — remove the disconnected legacy fact vocabulary.
--
-- RECORDS owns extraction through the immutable generation ontology (§0.4). The old
-- vocabulary tables and API were left behind by the retired fact/edge pipeline and no
-- longer influenced extraction, so retaining editable state was actively misleading.
BEGIN;

DROP FUNCTION IF EXISTS reader_vocabulary(UUID, INT);

DROP TRIGGER IF EXISTS initialize_novel_vocabulary ON novel;
DROP FUNCTION IF EXISTS initialize_novel_vocabulary();
DROP FUNCTION IF EXISTS seed_novel_vocabulary(UUID);

DROP TRIGGER IF EXISTS novel_vocabulary_alias_cycle ON novel_vocabulary_alias;
DROP FUNCTION IF EXISTS guard_novel_vocabulary_alias_cycle();
DROP FUNCTION IF EXISTS canonical_novel_vocabulary_name(UUID, TEXT, TEXT, INT);

DROP TABLE IF EXISTS novel_vocabulary_alias;
DROP TABLE IF EXISTS novel_vocabulary_chapter;
DROP TABLE IF EXISTS novel_vocabulary_changelog;
DROP TABLE IF EXISTS novel_vocabulary;

DROP FUNCTION IF EXISTS normalize_novel_vocabulary_name(TEXT);

COMMIT;
