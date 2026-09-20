-- §0.3 / §15: account authorization is independent of the chapter knowledge boundary.
BEGIN;
CREATE TABLE account (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 email text UNIQUE,
 google_subject text UNIQUE,
 status text NOT NULL DEFAULT 'active' CHECK(status IN ('active','disabled','deleting')),
 created_at timestamptz NOT NULL DEFAULT now()
);
-- Explicit staging owner, never assigned to the first person who signs in.
INSERT INTO account(id) VALUES ('00000000-0000-4000-8000-000000000001');
CREATE TABLE account_invitation (
 token_hash text PRIMARY KEY, email text NOT NULL,
 expires_at timestamptz NOT NULL, consumed_at timestamptz,
 account_id uuid REFERENCES account(id) ON DELETE CASCADE,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE account_session (
 token_hash text PRIMARY KEY, account_id uuid NOT NULL REFERENCES account(id) ON DELETE CASCADE,
 csrf_token text NOT NULL, expires_at timestamptz NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX account_session_owner ON account_session(account_id);
CREATE TABLE account_oauth_state (
 state_hash text PRIMARY KEY, nonce text NOT NULL, verifier text NOT NULL,
 invitation_hash text, expires_at timestamptz NOT NULL
);
CREATE FUNCTION current_account() RETURNS uuid LANGUAGE sql STABLE AS $$
 SELECT NULLIF(current_setting('app.account_id',true),'')::uuid
$$;
ALTER TABLE novel ADD COLUMN owner_id uuid NOT NULL DEFAULT '00000000-0000-4000-8000-000000000001' REFERENCES account(id);
ALTER TABLE novel ALTER COLUMN owner_id SET DEFAULT current_account();
CREATE INDEX novel_owner ON novel(owner_id,id);
ALTER TABLE provider_credential ADD COLUMN account_id uuid NOT NULL DEFAULT '00000000-0000-4000-8000-000000000001' REFERENCES account(id) ON DELETE CASCADE;
ALTER TABLE provider_credential ALTER COLUMN account_id SET DEFAULT current_account();
ALTER TABLE provider_credential DROP CONSTRAINT provider_credential_pkey;
ALTER TABLE provider_credential ADD PRIMARY KEY(account_id,provider);
ALTER TABLE provider_credential ADD COLUMN key_version int NOT NULL DEFAULT 0;
ALTER TABLE embedding_config ADD COLUMN account_id uuid NOT NULL DEFAULT '00000000-0000-4000-8000-000000000001' REFERENCES account(id) ON DELETE CASCADE;
ALTER TABLE embedding_config ALTER COLUMN account_id SET DEFAULT current_account();
ALTER TABLE embedding_config DROP CONSTRAINT embedding_config_pkey;
ALTER TABLE embedding_config ADD PRIMARY KEY(account_id);
CREATE TABLE account_cleanup (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 account_id uuid NOT NULL, novel_id uuid NOT NULL,
 attempts int NOT NULL DEFAULT 0, retry_at timestamptz NOT NULL DEFAULT now(),
 completed_at timestamptz, created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(novel_id)
);
CREATE TABLE account_usage (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 account_id uuid NOT NULL REFERENCES account(id) ON DELETE CASCADE,
 novel_id uuid, stage text NOT NULL, provider text NOT NULL, model text NOT NULL,
 input_tokens bigint, output_tokens bigint, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX account_usage_owner ON account_usage(account_id,created_at);
CREATE TABLE account_queue_control (
 account_id uuid PRIMARY KEY REFERENCES account(id) ON DELETE CASCADE,
 mode text NOT NULL DEFAULT 'all' CHECK(mode IN ('all','focused','paused')),
 focus_novel_id uuid REFERENCES novel(id) ON DELETE SET NULL,
 updated_at timestamptz NOT NULL DEFAULT now()
);
-- Separate auth and dispatch roles: neither browser queries nor stage writers get auth tables.
DO $$ BEGIN
 IF NOT EXISTS(SELECT FROM pg_roles WHERE rolname='book_auth') THEN CREATE ROLE book_auth NOLOGIN; END IF;
 IF NOT EXISTS(SELECT FROM pg_roles WHERE rolname='book_dispatcher') THEN CREATE ROLE book_dispatcher NOLOGIN; END IF;
END $$;
GRANT USAGE ON SCHEMA public TO book_auth,book_dispatcher;
GRANT SELECT,INSERT,UPDATE,DELETE ON account,account_invitation,account_session,account_oauth_state TO book_auth;
GRANT SELECT ON account,novel,chapter,job,scrape_job,account_queue_control TO book_dispatcher;
GRANT SELECT ON account TO ingest_writer,rls_reader,reader_progress_writer;
GRANT SELECT,INSERT,UPDATE,DELETE ON account_queue_control TO ingest_writer;
GRANT SELECT,INSERT ON account_usage TO ingest_writer;
GRANT SELECT,INSERT,UPDATE,DELETE ON account_cleanup TO ingest_writer;
GRANT USAGE ON SEQUENCE account_usage_id_seq,account_cleanup_id_seq TO ingest_writer;
GRANT SELECT ON account_queue_control TO rls_reader;
GRANT SELECT(owner_id) ON novel TO rls_reader,reader_progress_writer;
COMMIT;
