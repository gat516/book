-- §0.3 / §15. RESTRICTIVE ownership policies compose with existing spoiler policies
-- using AND, never permissive OR. No runtime role receives blanket BYPASSRLS.
BEGIN;
ALTER ROLE ingest_writer NOBYPASSRLS;
CREATE FUNCTION owns_novel(requested uuid) RETURNS boolean LANGUAGE sql STABLE AS $$
 SELECT EXISTS(SELECT 1 FROM novel WHERE id=requested AND owner_id=(SELECT current_account()))
$$;
CREATE FUNCTION account_active() RETURNS boolean LANGUAGE sql STABLE
 SET search_path=pg_catalog,public AS $$
 SELECT EXISTS(SELECT 1 FROM public.account WHERE id=public.current_account() AND status='active')
$$;
REVOKE ALL ON FUNCTION account_active() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION account_active() TO ingest_writer,rls_reader,reader_progress_writer,book_dispatcher;
ALTER TABLE novel ENABLE ROW LEVEL SECURITY;
ALTER TABLE novel FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_novel ON novel TO ingest_writer,rls_reader,reader_progress_writer
 USING(owner_id=(SELECT current_account()) AND (SELECT account_active()))
 WITH CHECK(owner_id=(SELECT current_account()) AND (SELECT account_active()));
CREATE POLICY dispatch_novel ON novel FOR SELECT TO book_dispatcher USING(true);
-- Grant stage writers explicit access; existing chapter policies still apply to readers.
DO $$ DECLARE t record; BEGIN
 FOR t IN SELECT table_name FROM information_schema.columns
   WHERE table_schema='public' AND column_name='novel_id'
     AND table_name NOT IN ('account_cleanup','account_usage')
 LOOP
  EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',t.table_name);
  EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',t.table_name);
  EXECUTE format('CREATE POLICY account_boundary ON %I AS RESTRICTIVE TO ingest_writer,rls_reader,reader_progress_writer USING (owns_novel(novel_id)) WITH CHECK (owns_novel(novel_id))',t.table_name);
  EXECUTE format('CREATE POLICY owned_stage_write ON %I TO ingest_writer USING (owns_novel(novel_id)) WITH CHECK (owns_novel(novel_id))',t.table_name);
  IF NOT EXISTS(SELECT FROM pg_policies WHERE schemaname='public' AND tablename=t.table_name AND permissive='PERMISSIVE' AND ('rls_reader'=ANY(roles) OR 'public'=ANY(roles))) THEN
   EXECUTE format('CREATE POLICY owned_metadata_read ON %I FOR SELECT TO rls_reader USING (owns_novel(novel_id))',t.table_name);
  END IF;
  EXECUTE format('CREATE POLICY owned_progress_access ON %I TO reader_progress_writer USING (owns_novel(novel_id)) WITH CHECK (owns_novel(novel_id))',t.table_name);
 END LOOP;
END $$;
CREATE POLICY dispatch_chapter ON chapter FOR SELECT TO book_dispatcher USING(true);
CREATE POLICY dispatch_job ON job FOR SELECT TO book_dispatcher USING(true);
CREATE POLICY dispatch_scrape ON scrape_job FOR SELECT TO book_dispatcher USING(true);
DO $$ DECLARE name text; BEGIN
 FOREACH name IN ARRAY ARRAY['provider_credential','embedding_config','account_queue_control','account_usage'] LOOP
  EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',name);
  EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',name);
  EXECUTE format('CREATE POLICY own_account ON %I TO ingest_writer,rls_reader,reader_progress_writer USING(account_id=(SELECT current_account()) AND (SELECT account_active())) WITH CHECK(account_id=(SELECT current_account()) AND (SELECT account_active()))',name);
 END LOOP;
END $$;
CREATE POLICY dispatch_controls ON account_queue_control FOR SELECT TO book_dispatcher USING(true);
-- A database function with a privileged owner must not expose another account's book.
-- The runtime reader also must not read unrelated account email/identity rows.
ALTER TABLE account ENABLE ROW LEVEL SECURITY;
ALTER TABLE account FORCE ROW LEVEL SECURITY;
CREATE POLICY own_identity ON account FOR SELECT TO ingest_writer,rls_reader,reader_progress_writer USING(id=(SELECT current_account()));
CREATE POLICY auth_identity ON account TO book_auth USING(true) WITH CHECK(true);
CREATE POLICY dispatch_accounts ON account FOR SELECT TO book_dispatcher USING(true);
COMMIT;
