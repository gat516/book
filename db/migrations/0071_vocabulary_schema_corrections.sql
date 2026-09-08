-- 0071_vocabulary_schema_corrections.sql — forward corrections to 0070 (§0.2, C.4).
--
-- Keep the chapter ledger's specified PK: kind widening is valid evidence in one chapter;
-- admission code counts distinct chapter_index values rather than raw rows.
BEGIN;

ALTER TABLE novel_vocabulary_chapter
  DROP CONSTRAINT novel_vocabulary_chapter_novel_id_term_type_name_chapter_in_key;

-- Alias targets must be canonical terms or aliases already visible at the correction's
-- knowledge chapter. Historical/future-only labels must not resolve at an earlier chapter.
CREATE OR REPLACE FUNCTION guard_novel_vocabulary_alias_cycle()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
  current_name TEXT := NEW.name;
  next_name TEXT;
  visited TEXT[] := ARRAY[NEW.name]::TEXT[];
BEGIN
  IF NEW.surface = NEW.name THEN
    RAISE EXCEPTION 'vocabulary alias cannot target itself';
  END IF;
  IF NOT EXISTS (
       SELECT 1 FROM novel_vocabulary v
        WHERE v.novel_id=NEW.novel_id
          AND v.term_type=NEW.term_type
          AND v.name=NEW.name
     ) AND NOT EXISTS (
       SELECT 1 FROM novel_vocabulary_alias a
        WHERE a.novel_id=NEW.novel_id
          AND a.term_type=NEW.term_type
          AND a.surface=NEW.name
          AND a.known_from_chapter <= NEW.known_from_chapter
     ) THEN
    RAISE EXCEPTION 'vocabulary alias target %.% is not visible at chapter %',
      NEW.term_type, NEW.name, NEW.known_from_chapter;
  END IF;
  LOOP
    SELECT a.name INTO next_name
      FROM novel_vocabulary_alias a
     WHERE a.novel_id = NEW.novel_id
       AND a.term_type = NEW.term_type
       AND a.surface = current_name
       AND a.known_from_chapter <= NEW.known_from_chapter
     ORDER BY a.known_from_chapter DESC, a.created_at DESC, a.id DESC
     LIMIT 1;
    EXIT WHEN NOT FOUND;
    IF next_name = NEW.surface OR next_name = ANY(visited) THEN
      RAISE EXCEPTION 'vocabulary alias cycle for %.%', NEW.term_type, NEW.surface;
    END IF;
    visited := visited || next_name;
    current_name := next_name;
  END LOOP;
  RETURN NEW;
END
$$;

COMMIT;
