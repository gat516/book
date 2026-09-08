-- 0078_reseed_novel_vocabulary_function.sql — converge seed_novel_vocabulary with the
-- body 0070 actually declares.
--
-- 0070 was applied to at least one database, and its seed_novel_vocabulary() was edited
-- afterwards to add the `jsonb_array_length(...) > 0` ontology-kinds guard. Because 0070
-- was already in schema_migrations, the runner would never re-apply it, so any database
-- migrated before that edit kept the unguarded function: every new novel silently got the
-- full 24-row default vocabulary regardless of its ontology (§0.4 — the seed is per-novel
-- data, so seeding kinds a novel does not declare is a real correctness bug, not cosmetic).
--
-- A fresh `migrate.sh` run was already correct; this exists so an already-migrated
-- database converges too, instead of depending on a hand-run psql nobody recorded.
-- CREATE OR REPLACE makes it idempotent and harmless where the guard is already present.
BEGIN;

CREATE OR REPLACE FUNCTION public.seed_novel_vocabulary(p_novel_id uuid)
 RETURNS void
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM novel WHERE id = p_novel_id) THEN
    RETURN;
  END IF;

  -- The default set is deliberately broad over the novel's existing kinds.  The later
  -- proposal validator narrows a term to compatible source/destination kinds; this seed
  -- cannot invent a kind that is absent from novel.ontology.
  WITH defaults(name, cardinality, gloss) AS (VALUES
    ('status','single','durable lifecycle/status; not a transient scene condition'),
    ('appearance','accretive','lasting physical description; not what they are wearing right now'),
    ('personality','accretive','durable personality tendency; not a mood in this scene'),
    ('ability','accretive','durable capability; not an action performed once'),
    ('affiliation','single','durable membership or allegiance; not merely sharing a location'),
    ('role','single','durable role or office; not a one-scene action'),
    ('origin','single','where the entity comes from; not where it is standing in this scene'),
    ('location','single','where the entity is based or belongs; not where they are standing in this scene'),
    ('possession','accretive','durable ownership; not an object briefly held'),
    ('reputation','accretive','durable reputation held by others; not a single reaction'),
    ('danger','accretive','durable danger or threat status; not a momentary attack'),
    ('description','accretive','only when no more specific attribute applies')
  )
  INSERT INTO novel_vocabulary
    (novel_id,term_type,name,kinds,dst_kinds,cardinality,polarity,status,gloss,
     proposals,first_seen_chapter,last_seen_chapter,admitted_at_chapter)
  SELECT p_novel_id,'attribute',d.name,
         COALESCE((SELECT ARRAY_AGG(k ORDER BY k)
                     FROM jsonb_array_elements_text(n.ontology->'kinds') k), '{}'),
         '{}',d.cardinality,0,'admitted',d.gloss,0,0,0,0
    FROM defaults d CROSS JOIN novel n
    WHERE n.id=p_novel_id AND jsonb_array_length(COALESCE(n.ontology->'kinds','[]'::jsonb)) > 0
  ON CONFLICT(novel_id,term_type,name) DO UPDATE SET
    kinds = ARRAY(SELECT DISTINCT k FROM unnest(novel_vocabulary.kinds || EXCLUDED.kinds) k ORDER BY k),
    gloss = CASE WHEN novel_vocabulary.gloss='' THEN EXCLUDED.gloss ELSE novel_vocabulary.gloss END,
    status = CASE WHEN novel_vocabulary.status='candidate' THEN 'admitted' ELSE novel_vocabulary.status END,
    admitted_at_chapter = COALESCE(novel_vocabulary.admitted_at_chapter,0),
    updated_at = now();

  WITH defaults(name, polarity) AS (VALUES
    ('ally',1),('enemy',-1),('family',0),('mentor',1),('member_of',0),('located_in',0),
    ('romantic',1),('married_to',1),('rival',-1),('subordinate_of',0),
    ('distrusts',-1),('plots_against',-1)
  )
  INSERT INTO novel_vocabulary
    (novel_id,term_type,name,kinds,dst_kinds,cardinality,polarity,status,gloss,
     proposals,first_seen_chapter,last_seen_chapter,admitted_at_chapter)
  SELECT p_novel_id,'relation',d.name,
         COALESCE((SELECT ARRAY_AGG(k ORDER BY k)
                     FROM jsonb_array_elements_text(n.ontology->'kinds') k), '{}'),
         COALESCE((SELECT ARRAY_AGG(k ORDER BY k)
                     FROM jsonb_array_elements_text(n.ontology->'kinds') k), '{}'),
         'accretive',d.polarity,'admitted',
         'durable relationship; record only when it remains true after this scene',
         0,0,0,0
    FROM defaults d CROSS JOIN novel n
    WHERE n.id=p_novel_id AND jsonb_array_length(COALESCE(n.ontology->'kinds','[]'::jsonb)) > 0
  ON CONFLICT(novel_id,term_type,name) DO UPDATE SET
    kinds = ARRAY(SELECT DISTINCT k FROM unnest(novel_vocabulary.kinds || EXCLUDED.kinds) k ORDER BY k),
    dst_kinds = ARRAY(SELECT DISTINCT k FROM unnest(novel_vocabulary.dst_kinds || EXCLUDED.dst_kinds) k ORDER BY k),
    polarity = CASE WHEN novel_vocabulary.polarity=0 THEN EXCLUDED.polarity ELSE novel_vocabulary.polarity END,
    gloss = CASE WHEN novel_vocabulary.gloss='' THEN EXCLUDED.gloss ELSE novel_vocabulary.gloss END,
    status = CASE WHEN novel_vocabulary.status='candidate' THEN 'admitted' ELSE novel_vocabulary.status END,
    admitted_at_chapter = COALESCE(novel_vocabulary.admitted_at_chapter,0),
    updated_at = now();

  -- Existing ontology entries are admitted seeds too.  Object entries may restrict kinds;
  -- string entries inherit every configured kind.  Invalid/non-normalized names are left
  -- to the application proposal path rather than making migration fail on old data.
  WITH attrs AS (
    SELECT normalize_novel_vocabulary_name(
             CASE WHEN jsonb_typeof(a)='object' THEN a->>'name' ELSE a #>> '{}' END
           ) AS name,
           CASE WHEN jsonb_typeof(a)='object'
                     AND jsonb_typeof(a->'kinds')='array'
                THEN ARRAY(SELECT k FROM jsonb_array_elements_text(a->'kinds') k
                            WHERE n.ontology->'kinds' ? k)
                ELSE ARRAY(SELECT jsonb_array_elements_text(n.ontology->'kinds')) END AS kinds
      FROM novel n CROSS JOIN LATERAL jsonb_array_elements(COALESCE(n.ontology->'attributes','[]'::jsonb)) a
     WHERE n.id=p_novel_id
  )
  INSERT INTO novel_vocabulary
    (novel_id,term_type,name,kinds,dst_kinds,cardinality,polarity,status,gloss,
     proposals,first_seen_chapter,last_seen_chapter,admitted_at_chapter)
  SELECT p_novel_id,'attribute',a.name,a.kinds,'{}',
         CASE WHEN a.name IN ('status','affiliation','role','origin','location') THEN 'single' ELSE 'accretive' END,
         0,'admitted',format('durable %s; preserve the novel-specific evidence',a.name),0,0,0,0
    FROM attrs a
    WHERE a.name ~ '^[a-z][a-z0-9_]{1,39}$' AND cardinality(a.kinds) > 0
  ON CONFLICT(novel_id,term_type,name) DO UPDATE SET
    kinds = ARRAY(SELECT DISTINCT k FROM unnest(novel_vocabulary.kinds || EXCLUDED.kinds) k ORDER BY k),
    updated_at = now();

  WITH rels AS (
    SELECT normalize_novel_vocabulary_name(
             CASE WHEN jsonb_typeof(r)='object' THEN r->>'name' ELSE r #>> '{}' END
           ) AS name,
           CASE WHEN jsonb_typeof(r)='object'
                     AND jsonb_typeof(r->'src_kinds')='array'
                THEN ARRAY(SELECT k FROM jsonb_array_elements_text(r->'src_kinds') k
                            WHERE n.ontology->'kinds' ? k)
                WHEN jsonb_typeof(r)='object'
                     AND jsonb_typeof(r->'source_kinds')='array'
                THEN ARRAY(SELECT k FROM jsonb_array_elements_text(r->'source_kinds') k
                            WHERE n.ontology->'kinds' ? k)
                WHEN jsonb_typeof(r)='object'
                     AND jsonb_typeof(r->'kinds')='array'
                THEN ARRAY(SELECT k FROM jsonb_array_elements_text(r->'kinds') k
                            WHERE n.ontology->'kinds' ? k)
                ELSE ARRAY(SELECT jsonb_array_elements_text(n.ontology->'kinds')) END AS kinds,
           CASE WHEN jsonb_typeof(r)='object'
                     AND jsonb_typeof(r->'dst_kinds')='array'
                THEN ARRAY(SELECT k FROM jsonb_array_elements_text(r->'dst_kinds') k
                            WHERE n.ontology->'kinds' ? k)
                WHEN jsonb_typeof(r)='object'
                     AND jsonb_typeof(r->'target_kinds')='array'
                THEN ARRAY(SELECT k FROM jsonb_array_elements_text(r->'target_kinds') k
                            WHERE n.ontology->'kinds' ? k)
                WHEN jsonb_typeof(r)='object'
                     AND jsonb_typeof(r->'kinds')='array'
                THEN ARRAY(SELECT k FROM jsonb_array_elements_text(r->'kinds') k
                            WHERE n.ontology->'kinds' ? k)
                ELSE ARRAY(SELECT jsonb_array_elements_text(n.ontology->'kinds')) END AS dst_kinds
      FROM novel n CROSS JOIN LATERAL jsonb_array_elements(COALESCE(n.ontology->'relations','[]'::jsonb)) r
     WHERE n.id=p_novel_id
  )
  INSERT INTO novel_vocabulary
    (novel_id,term_type,name,kinds,dst_kinds,cardinality,polarity,status,gloss,
     proposals,first_seen_chapter,last_seen_chapter,admitted_at_chapter)
  SELECT p_novel_id,'relation',r.name,r.kinds,r.dst_kinds,'accretive',0,'admitted',
         'durable relationship; preserve the novel-specific evidence',0,0,0,0
    FROM rels r
    WHERE r.name ~ '^[a-z][a-z0-9_]{1,39}$'
      AND cardinality(r.kinds) > 0 AND cardinality(r.dst_kinds) > 0
  ON CONFLICT(novel_id,term_type,name) DO UPDATE SET
    kinds = ARRAY(SELECT DISTINCT k FROM unnest(novel_vocabulary.kinds || EXCLUDED.kinds) k ORDER BY k),
    dst_kinds = ARRAY(SELECT DISTINCT k FROM unnest(novel_vocabulary.dst_kinds || EXCLUDED.dst_kinds) k ORDER BY k),
    updated_at = now();
END
$function$

;

COMMIT;
