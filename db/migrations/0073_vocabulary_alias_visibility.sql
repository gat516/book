-- 0073_vocabulary_alias_visibility.sql — close chapter-visible alias gaps (§0.3, C.10).
BEGIN;

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
          AND v.first_seen_chapter <= NEW.known_from_chapter
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

CREATE OR REPLACE FUNCTION reader_vocabulary(requested_novel UUID, requested_chapter INT)
RETURNS TABLE(
  term_type   TEXT,
  name        TEXT,
  kinds       TEXT[],
  dst_kinds   TEXT[],
  cardinality TEXT,
  status      TEXT,
  polarity    SMALLINT,
  gloss       TEXT,
  aliases     TEXT[]
)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT v.term_type,
         v.name,
         v.kinds,
         v.dst_kinds,
         v.cardinality,
         v.status,
         v.polarity,
         COALESCE(
           (SELECT c.new_value->>'gloss'
              FROM novel_vocabulary_changelog c
             WHERE c.novel_id=v.novel_id
               AND c.term_type=v.term_type
               AND c.name=v.name
               AND c.changed_at_chapter <= requested_chapter
             ORDER BY c.changed_at_chapter DESC, c.seq DESC
             LIMIT 1),
           (SELECT c.old_value->>'gloss'
              FROM novel_vocabulary_changelog c
             WHERE c.novel_id=v.novel_id
               AND c.term_type=v.term_type
               AND c.name=v.name
               AND c.changed_at_chapter > requested_chapter
             ORDER BY c.changed_at_chapter ASC, c.seq ASC
             LIMIT 1),
           v.gloss
         ) AS gloss,
         COALESCE(
           (SELECT array_agg(resolved.surface ORDER BY resolved.surface)
              FROM (
                SELECT DISTINCT ON (a.surface)
                       a.surface,
                       canonical_novel_vocabulary_name(
                         a.novel_id,a.term_type,a.surface,requested_chapter) AS canonical_name
                  FROM novel_vocabulary_alias a
                 WHERE a.novel_id=v.novel_id
                   AND a.term_type=v.term_type
                   AND a.known_from_chapter <= requested_chapter
                 ORDER BY a.surface, a.known_from_chapter DESC, a.created_at DESC, a.id DESC
              ) resolved
              JOIN novel_vocabulary target
                ON target.novel_id=v.novel_id
               AND target.term_type=v.term_type
               AND target.name=resolved.canonical_name
               AND target.first_seen_chapter <= requested_chapter
             WHERE resolved.canonical_name=v.name),
           '{}'::TEXT[]
         ) AS aliases
    FROM novel_vocabulary v
   WHERE requested_novel = reader_novel()
     AND requested_chapter >= 0
     AND requested_chapter <= reader_chapter()
     AND v.novel_id = requested_novel
     AND v.first_seen_chapter <= requested_chapter
   ORDER BY v.term_type, v.name
$$;

ALTER FUNCTION reader_vocabulary(UUID, INT) OWNER TO repair_reporter;
REVOKE EXECUTE ON FUNCTION reader_vocabulary(UUID, INT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION reader_vocabulary(UUID, INT) TO rls_reader;

COMMIT;
