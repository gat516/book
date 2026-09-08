-- 0075_reader_vocabulary_canonical_rows.sql — hide remapped historical rows (§0.3, C.7).
--
-- A vocabulary rename is an alias, not a mutation. Once a surface is remapped, the old
-- canonical row must not remain a second reader-visible label at later chapters.
BEGIN;

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
     AND canonical_novel_vocabulary_name(
           v.novel_id,v.term_type,v.name,requested_chapter) IS NOT DISTINCT FROM v.name
   ORDER BY v.term_type, v.name
$$;

ALTER FUNCTION reader_vocabulary(UUID, INT) OWNER TO repair_reporter;
REVOKE EXECUTE ON FUNCTION reader_vocabulary(UUID, INT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION reader_vocabulary(UUID, INT) TO rls_reader;

COMMIT;
