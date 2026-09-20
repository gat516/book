\set ON_ERROR_STOP on
BEGIN;
INSERT INTO account(id,email) VALUES ('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa','a@example.test'),('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb','b@example.test');
INSERT INTO novel(id,owner_id,title,source_lang,target_lang,ontology) VALUES
('aaaaaaaa-0000-4000-8000-000000000001','aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa','A','zh','en','{}'),
('bbbbbbbb-0000-4000-8000-000000000001','bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb','B','zh','en','{}');
INSERT INTO chapter(novel_id,chapter_index,raw_hash,raw_uri,source_meta) SELECT n.id,c,c::text,'test','{}'::jsonb FROM novel n CROSS JOIN generate_series(1,2) c WHERE n.title IN ('A','B');
INSERT INTO chapter_fact(novel_id,chapter_index,prompt_version,ordinal,text,source_hash,requested_model) SELECT n.id,c,'test',1,n.title||c,'hash','test' FROM novel n CROSS JOIN generate_series(1,2) c WHERE n.title IN ('A','B');
INSERT INTO glossary(novel_id,source_term,target_term,locked_at_chapter) SELECT n.id,c::text,n.title||c,c FROM novel n CROSS JOIN generate_series(1,2) c WHERE n.title IN ('A','B');
SET LOCAL ROLE rls_reader;
SELECT set_config('app.account_id','aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',true);
DO $$ BEGIN
 IF (SELECT count(*) FROM novel)<>1 THEN RAISE EXCEPTION 'account boundary failed'; END IF;
 IF EXISTS(SELECT FROM novel WHERE title='B') THEN RAISE EXCEPTION 'cross-account read'; END IF;
END $$;
SELECT set_config('app.novel_id','aaaaaaaa-0000-4000-8000-000000000001',true),set_config('app.current_chapter','1',true);
DO $$ BEGIN
 IF (SELECT count(*) FROM chapter_fact)<>1 OR (SELECT count(*) FROM glossary)<>1 THEN RAISE EXCEPTION 'spoiler gate failed'; END IF;
END $$;
SELECT set_config('app.novel_id','bbbbbbbb-0000-4000-8000-000000000001',true),set_config('app.current_chapter','999',true);
DO $$ BEGIN IF EXISTS(SELECT FROM chapter_fact) OR EXISTS(SELECT FROM glossary) THEN RAISE EXCEPTION 'forged gate leaked other owner'; END IF; END $$;
SELECT set_config('app.account_id','',true);
DO $$ BEGIN IF EXISTS(SELECT FROM novel) THEN RAISE EXCEPTION 'unset account did not fail closed'; END IF; END $$;
RESET ROLE;
SET LOCAL ROLE ingest_writer;
SELECT set_config('app.account_id','aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',true);
DO $$ BEGIN
 UPDATE novel SET title='stolen' WHERE id='bbbbbbbb-0000-4000-8000-000000000001';
 IF FOUND THEN RAISE EXCEPTION 'cross-account write'; END IF;
 BEGIN
  INSERT INTO novel(owner_id,title,source_lang,target_lang,ontology) VALUES ('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb','stolen','zh','en','{}');
  RAISE EXCEPTION 'cross-account insert';
 EXCEPTION WHEN insufficient_privilege THEN NULL; END;
END $$;
RESET ROLE;
SET LOCAL ROLE book_worker;
SELECT set_config('app.account_id','aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',true);
DO $$ BEGIN
 IF (SELECT count(*) FROM worker_catalog())<2 THEN RAISE EXCEPTION 'dispatcher cannot route accounts'; END IF;
 IF (SELECT count(*) FROM chapter_fact)<>2 THEN RAISE EXCEPTION 'worker owner scope failed'; END IF;
END $$;
RESET ROLE;
SET LOCAL ROLE book_auth;
SELECT request_account_deletion('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa');
RESET ROLE;
SET LOCAL ROLE book_worker;
DO $$ BEGIN
 IF worker_novel_owner('aaaaaaaa-0000-4000-8000-000000000001') IS NOT NULL THEN RAISE EXCEPTION 'deleting account dispatchable'; END IF;
 IF EXISTS(SELECT FROM chapter_fact) THEN RAISE EXCEPTION 'deleted account readable'; END IF;
END $$;
RESET ROLE;
DO $$ BEGIN IF NOT EXISTS(SELECT FROM account_cleanup WHERE account_id='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa') THEN RAISE EXCEPTION 'missing deletion outbox'; END IF; END $$;
ROLLBACK;
