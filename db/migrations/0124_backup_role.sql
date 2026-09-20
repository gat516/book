-- §15: logical backups use an explicit read-only role, compatible with RDS (no BYPASSRLS).
BEGIN;
DO $$ BEGIN IF NOT EXISTS(SELECT FROM pg_roles WHERE rolname='book_backup') THEN CREATE ROLE book_backup NOLOGIN; END IF; END $$;
GRANT USAGE ON SCHEMA public TO book_backup;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO book_backup;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO book_backup;
DO $$ DECLARE t record; BEGIN
 FOR t IN SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind='r' AND c.relrowsecurity LOOP
  EXECUTE format('CREATE POLICY backup_read ON %I FOR SELECT TO book_backup USING(true)',t.relname);
 END LOOP;
END $$;
COMMIT;
