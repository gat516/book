-- §15: workers may discover routing metadata, then scope all content work to its owner.
BEGIN;
DO $$ BEGIN IF NOT EXISTS(SELECT FROM pg_roles WHERE rolname='book_worker') THEN CREATE ROLE book_worker NOLOGIN; END IF; END $$;
GRANT ingest_writer TO book_worker;
CREATE FUNCTION worker_novel_owner(requested uuid) RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER
 SET search_path=pg_catalog,public AS $$
 SELECT n.owner_id FROM public.novel n JOIN public.account a ON a.id=n.owner_id WHERE n.id=requested AND a.status='active'
$$;
CREATE FUNCTION worker_catalog() RETURNS TABLE(novel_id uuid,account_id uuid,mode text,focus_novel_id uuid)
 LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
 SELECT n.id,n.owner_id,COALESCE(c.mode,'all'),c.focus_novel_id FROM public.novel n
 JOIN public.account a ON a.id=n.owner_id AND a.status='active'
 LEFT JOIN public.account_queue_control c ON c.account_id=a.id
$$;
CREATE FUNCTION worker_due_retries(generic_limit int,provider_limit int)
 RETURNS TABLE(novel_id uuid,chapter_index int,status text,translation_ready boolean)
 LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
 SELECT c.novel_id,c.chapter_index,c.status,c.translation_ready FROM public.chapter c
 JOIN public.novel n ON n.id=c.novel_id JOIN public.account a ON a.id=n.owner_id AND a.status='active'
 WHERE ((NOT c.enrichment_discarded AND c.enrichment_retry_at<=now() AND c.enrichment_attempts<generic_limit
 AND c.provider_retry_at IS NULL AND c.provider_retry_attempts<provider_limit)
 OR (c.provider_retry_at<=now() AND c.provider_retry_attempts<provider_limit AND (NOT c.translation_ready OR NOT c.enrichment_discarded)))
 ORDER BY LEAST(COALESCE(c.enrichment_retry_at,'infinity'),COALESCE(c.provider_retry_at,'infinity')) LIMIT 100
$$;
GRANT CREATE ON SCHEMA public TO book_dispatcher;
ALTER FUNCTION worker_novel_owner(uuid) OWNER TO book_dispatcher;
ALTER FUNCTION worker_catalog() OWNER TO book_dispatcher;
ALTER FUNCTION worker_due_retries(int,int) OWNER TO book_dispatcher;
REVOKE CREATE ON SCHEMA public FROM book_dispatcher;
REVOKE ALL ON FUNCTION worker_novel_owner(uuid),worker_catalog(),worker_due_retries(int,int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION worker_novel_owner(uuid),worker_catalog(),worker_due_retries(int,int) TO book_worker;
COMMIT;
BEGIN;
GRANT SELECT ON account_queue_control TO book_dispatcher;
CREATE POLICY dispatch_control ON account_queue_control FOR SELECT TO book_dispatcher USING(true);
CREATE FUNCTION worker_scrape_catalog() RETURNS TABLE(job_id bigint,novel_id uuid,account_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
SELECT s.id,s.novel_id,n.owner_id FROM public.scrape_job s JOIN public.novel n ON n.id=s.novel_id
JOIN public.account a ON a.id=n.owner_id WHERE a.status='active' AND s.status IN ('pending','running') ORDER BY s.id
$$;
GRANT CREATE ON SCHEMA public TO book_dispatcher;
ALTER FUNCTION worker_scrape_catalog() OWNER TO book_dispatcher;
REVOKE CREATE ON SCHEMA public FROM book_dispatcher;
REVOKE ALL ON FUNCTION worker_scrape_catalog() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION worker_scrape_catalog() TO book_worker;
COMMIT;
