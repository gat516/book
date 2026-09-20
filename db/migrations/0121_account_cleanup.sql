-- §15: deletion authorization is immediate; cross-store cleanup is a durable outbox.
BEGIN;
CREATE ROLE book_cleanup NOLOGIN;
GRANT USAGE ON SCHEMA public TO book_cleanup;
GRANT SELECT,DELETE ON novel TO book_cleanup;
GRANT SELECT,UPDATE,DELETE ON account TO book_cleanup;
GRANT SELECT,INSERT,UPDATE,DELETE ON account_cleanup TO book_cleanup;
GRANT USAGE,SELECT ON SEQUENCE account_cleanup_id_seq TO book_cleanup;
GRANT SELECT ON reader_progress TO ingest_writer;
CREATE POLICY cleanup_novel ON novel TO book_cleanup USING(true);
CREATE POLICY cleanup_identity ON account TO book_cleanup USING(true);
ALTER TABLE account_cleanup ENABLE ROW LEVEL SECURITY;
ALTER TABLE account_cleanup FORCE ROW LEVEL SECURITY;
CREATE POLICY own_cleanup ON account_cleanup TO ingest_writer USING(account_id=current_account()) WITH CHECK(account_id=current_account());
CREATE POLICY cleanup_outbox ON account_cleanup TO book_cleanup USING(true) WITH CHECK(true);
CREATE FUNCTION enqueue_novel_cleanup() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
BEGIN
 INSERT INTO public.account_cleanup(account_id,novel_id,retry_at) VALUES(OLD.owner_id,OLD.id,now()+interval '5 minutes') ON CONFLICT(novel_id) DO NOTHING;
 RETURN OLD;
END $$;
CREATE TRIGGER novel_cleanup BEFORE DELETE ON novel FOR EACH ROW EXECUTE FUNCTION enqueue_novel_cleanup();
CREATE FUNCTION request_account_deletion(actor uuid) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
BEGIN
 UPDATE public.account SET status='deleting' WHERE id=actor;
 INSERT INTO public.account_cleanup(account_id,novel_id,retry_at)
 SELECT actor,id,now()+interval '5 minutes' FROM public.novel WHERE owner_id=actor ON CONFLICT(novel_id) DO NOTHING;
END $$;
GRANT CREATE ON SCHEMA public TO book_cleanup;
ALTER FUNCTION request_account_deletion(uuid) OWNER TO book_cleanup;
REVOKE CREATE ON SCHEMA public FROM book_cleanup;
REVOKE ALL ON FUNCTION request_account_deletion(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION request_account_deletion(uuid) TO book_auth;
-- Usage contains metadata only and is independently scoped by account RLS.
GRANT SELECT,INSERT ON account_usage TO rls_reader;
GRANT USAGE ON SEQUENCE account_usage_id_seq TO rls_reader;
COMMIT;
