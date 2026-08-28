-- Keep tombstones so human deletions cannot be silently promoted again by RESOLVE.
-- Versions and the append-only audit chain survive deletion (§0, §7.2).
ALTER TABLE glossary ADD COLUMN deleted BOOLEAN NOT NULL DEFAULT false;
DROP INDEX glossary_novel_target_key;
CREATE UNIQUE INDEX glossary_novel_target_key ON glossary (novel_id, target_term)
  WHERE NOT deleted;
