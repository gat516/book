-- Portable RDS-compatible roles. No superuser or BYPASSRLS attributes are required.
DO $$ DECLARE name text; BEGIN
 FOREACH name IN ARRAY ARRAY['ingest_writer','rls_reader','reader_progress_writer','book_auth','book_dispatcher','book_worker','book_cleanup','book_backup','book_maintenance'] LOOP
  IF NOT EXISTS(SELECT FROM pg_roles WHERE rolname=name) THEN EXECUTE format('CREATE ROLE %I NOLOGIN',name); END IF;
  -- Migration/operator principal may assign function ownership; never grant this to an app login.
  EXECUTE format('GRANT %I TO %I',name,current_user);
 END LOOP;
END $$;
GRANT ingest_writer TO book_worker;
