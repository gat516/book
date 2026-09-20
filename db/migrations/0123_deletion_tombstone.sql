-- §15: restores must replay deletion intent newer than their snapshot.
BEGIN;
CREATE TABLE account_deletion_tombstone(account_id uuid PRIMARY KEY,requested_at timestamptz NOT NULL DEFAULT now());
REVOKE ALL ON account_deletion_tombstone FROM PUBLIC;
GRANT SELECT,INSERT ON account_deletion_tombstone TO book_cleanup;
CREATE OR REPLACE FUNCTION request_account_deletion(actor uuid) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
BEGIN
 INSERT INTO public.account_deletion_tombstone(account_id) VALUES(actor) ON CONFLICT DO NOTHING;
 UPDATE public.account SET status='deleting' WHERE id=actor;
 INSERT INTO public.account_cleanup(account_id,novel_id,retry_at)
 SELECT actor,id,now()+interval '5 minutes' FROM public.novel WHERE owner_id=actor ON CONFLICT(novel_id) DO NOTHING;
END $$;
COMMIT;
