-- §15: offline data maintenance must also work on RDS without superuser/BYPASSRLS.
-- Never grant this role to an application login. SET ROLE avoids inheriting the
-- restrictive runtime account policies while retaining explicit RLS authorization.
BEGIN;
DO $$ BEGIN IF NOT EXISTS(SELECT FROM pg_roles WHERE rolname='book_maintenance') THEN CREATE ROLE book_maintenance NOLOGIN; END IF; END $$;
GRANT USAGE ON SCHEMA public TO book_maintenance;
GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO book_maintenance;
GRANT USAGE,SELECT,UPDATE ON ALL SEQUENCES IN SCHEMA public TO book_maintenance;
GRANT EXECUTE ON FUNCTION request_account_deletion(uuid) TO book_maintenance;
DO $$ DECLARE t record; BEGIN
 FOR t IN SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind='r' AND c.relrowsecurity LOOP
  EXECUTE format('CREATE POLICY maintenance_access ON %I TO book_maintenance USING(true) WITH CHECK(true)',t.relname);
 END LOOP;
 EXECUTE format('GRANT book_maintenance TO %I',session_user);
END $$;
COMMIT;
