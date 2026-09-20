\set ON_ERROR_STOP on
BEGIN;
INSERT INTO account(id,email) VALUES ('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa','a@example.test'),('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb','b@example.test');
INSERT INTO novel(id,owner_id,title,source_lang,target_lang,ontology) VALUES
('aaaaaaaa-0000-4000-8000-000000000001','aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa','A','zh','en','{}'),
('bbbbbbbb-0000-4000-8000-000000000001','bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb','B','zh','en','{}');
SET LOCAL ROLE rls_reader;
SELECT set_config('app.account_id','aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',true);
DO $$ BEGIN
 IF (SELECT count(*) FROM novel)<>1 THEN RAISE EXCEPTION 'account boundary failed'; END IF;
 IF EXISTS(SELECT FROM novel WHERE title='B') THEN RAISE EXCEPTION 'cross-account read'; END IF;
END $$;
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
ROLLBACK;
