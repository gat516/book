-- Generated from all migrations through 0122; schema only, no user data. §15.
--
-- PostgreSQL database dump
--


-- Dumped from database version 16.14 (Debian 16.14-1.pgdg12+1)
-- Dumped by pg_dump version 16.14 (Debian 16.14-1.pgdg12+1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: vector; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;


--
-- Name: EXTENSION vector; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION vector IS 'vector data type and ivfflat and hnsw access methods';


--
-- Name: account_active(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.account_active() RETURNS boolean
    LANGUAGE sql STABLE
    SET search_path TO 'pg_catalog', 'public'
    AS $$
 SELECT EXISTS(SELECT 1 FROM public.account WHERE id=public.current_account() AND status='active')
$$;


--
-- Name: current_account(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.current_account() RETURNS uuid
    LANGUAGE sql STABLE
    AS $$
 SELECT NULLIF(current_setting('app.account_id',true),'')::uuid
$$;


--
-- Name: enqueue_novel_cleanup(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.enqueue_novel_cleanup() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'public'
    AS $$
BEGIN
 INSERT INTO public.account_cleanup(account_id,novel_id,retry_at) VALUES(OLD.owner_id,OLD.id,now()+interval '5 minutes') ON CONFLICT(novel_id) DO NOTHING;
 RETURN OLD;
END $$;


--
-- Name: guard_chapter_fact_immutability(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.guard_chapter_fact_immutability() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
  RAISE EXCEPTION 'chapter_fact rows are immutable';
END $$;


--
-- Name: guard_edge_supersession(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.guard_edge_supersession() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE target_novel UUID; found_cycle BOOLEAN;
BEGIN
  IF NEW.supersedes IS NULL THEN
    RETURN NEW;
  END IF;
  SELECT e.novel_id INTO target_novel FROM edge e WHERE e.id=NEW.supersedes;
  IF NOT FOUND OR target_novel IS DISTINCT FROM NEW.novel_id THEN
    RAISE EXCEPTION 'edge supersession must target an edge in the same novel';
  END IF;
  WITH RECURSIVE chain(id) AS (
    SELECT NEW.supersedes
    UNION ALL
    SELECT e.supersedes FROM edge e JOIN chain c ON e.id=c.id
     WHERE e.supersedes IS NOT NULL
  )
  SELECT EXISTS (SELECT 1 FROM chain WHERE id=NEW.id) INTO found_cycle;
  IF found_cycle THEN
    RAISE EXCEPTION 'edge supersession cycle for edge %', NEW.id;
  END IF;
  RETURN NEW;
END
$$;


--
-- Name: guard_wiki_page_immutability(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.guard_wiki_page_immutability() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
  RAISE EXCEPTION 'wiki_page rows are immutable';
END $$;


--
-- Name: owns_novel(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.owns_novel(requested uuid) RETURNS boolean
    LANGUAGE sql STABLE
    AS $$
 SELECT EXISTS(SELECT 1 FROM novel WHERE id=requested AND owner_id=(SELECT current_account()))
$$;


--
-- Name: reader_chapter(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.reader_chapter() RETURNS integer
    LANGUAGE sql STABLE
    AS $$ SELECT COALESCE(NULLIF(current_setting('app.current_chapter', true), '')::int, -1) $$;


--
-- Name: reader_novel(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.reader_novel() RETURNS uuid
    LANGUAGE sql STABLE
    AS $$ SELECT NULLIF(current_setting('app.novel_id', true), '')::uuid $$;


--
-- Name: request_account_deletion(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.request_account_deletion(actor uuid) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
BEGIN
 UPDATE public.account SET status='deleting' WHERE id=actor;
 INSERT INTO public.account_cleanup(account_id,novel_id,retry_at)
 SELECT actor,id,now()+interval '5 minutes' FROM public.novel WHERE owner_id=actor ON CONFLICT(novel_id) DO NOTHING;
END $$;


--
-- Name: worker_catalog(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.worker_catalog() RETURNS TABLE(novel_id uuid, account_id uuid, mode text, focus_novel_id uuid)
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
 SELECT n.id,n.owner_id,COALESCE(c.mode,'all'),c.focus_novel_id FROM public.novel n
 JOIN public.account a ON a.id=n.owner_id AND a.status='active'
 LEFT JOIN public.account_queue_control c ON c.account_id=a.id
$$;


--
-- Name: worker_due_retries(integer, integer); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.worker_due_retries(generic_limit integer, provider_limit integer) RETURNS TABLE(novel_id uuid, chapter_index integer, status text, translation_ready boolean)
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
 SELECT c.novel_id,c.chapter_index,c.status,c.translation_ready FROM public.chapter c
 JOIN public.novel n ON n.id=c.novel_id JOIN public.account a ON a.id=n.owner_id AND a.status='active'
 WHERE ((NOT c.enrichment_discarded AND c.enrichment_retry_at<=now() AND c.enrichment_attempts<generic_limit
 AND c.provider_retry_at IS NULL AND c.provider_retry_attempts<provider_limit)
 OR (c.provider_retry_at<=now() AND c.provider_retry_attempts<provider_limit AND (NOT c.translation_ready OR NOT c.enrichment_discarded)))
 ORDER BY LEAST(COALESCE(c.enrichment_retry_at,'infinity'),COALESCE(c.provider_retry_at,'infinity')) LIMIT 100
$$;


--
-- Name: worker_novel_owner(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.worker_novel_owner(requested uuid) RETURNS uuid
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
 SELECT n.owner_id FROM public.novel n JOIN public.account a ON a.id=n.owner_id WHERE n.id=requested AND a.status='active'
$$;


--
-- Name: worker_scrape_catalog(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.worker_scrape_catalog() RETURNS TABLE(job_id bigint, novel_id uuid, account_id uuid)
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
SELECT s.id,s.novel_id,n.owner_id FROM public.scrape_job s JOIN public.novel n ON n.id=s.novel_id
JOIN public.account a ON a.id=n.owner_id WHERE a.status='active' AND s.status IN ('pending','running') ORDER BY s.id
$$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: account; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.account (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    email text,
    google_subject text,
    status text DEFAULT 'active'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT account_status_check CHECK ((status = ANY (ARRAY['active'::text, 'disabled'::text, 'deleting'::text])))
);

ALTER TABLE ONLY public.account FORCE ROW LEVEL SECURITY;


--
-- Name: account_cleanup; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.account_cleanup (
    id bigint NOT NULL,
    account_id uuid NOT NULL,
    novel_id uuid NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    retry_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.account_cleanup FORCE ROW LEVEL SECURITY;


--
-- Name: account_cleanup_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.account_cleanup ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.account_cleanup_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: account_invitation; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.account_invitation (
    token_hash text NOT NULL,
    email text NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    consumed_at timestamp with time zone,
    account_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: account_oauth_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.account_oauth_state (
    state_hash text NOT NULL,
    nonce text NOT NULL,
    verifier text NOT NULL,
    invitation_hash text,
    expires_at timestamp with time zone NOT NULL
);


--
-- Name: account_queue_control; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.account_queue_control (
    account_id uuid NOT NULL,
    mode text DEFAULT 'all'::text NOT NULL,
    focus_novel_id uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT account_queue_control_mode_check CHECK ((mode = ANY (ARRAY['all'::text, 'focused'::text, 'paused'::text])))
);

ALTER TABLE ONLY public.account_queue_control FORCE ROW LEVEL SECURITY;


--
-- Name: account_session; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.account_session (
    token_hash text NOT NULL,
    account_id uuid NOT NULL,
    csrf_token text NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: account_usage; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.account_usage (
    id bigint NOT NULL,
    account_id uuid NOT NULL,
    novel_id uuid,
    stage text NOT NULL,
    provider text NOT NULL,
    model text NOT NULL,
    input_tokens bigint,
    output_tokens bigint,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.account_usage FORCE ROW LEVEL SECURITY;


--
-- Name: account_usage_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.account_usage ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.account_usage_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: chapter; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chapter (
    novel_id uuid NOT NULL,
    chapter_index integer NOT NULL,
    raw_hash text NOT NULL,
    raw_uri text NOT NULL,
    translated_uri text,
    glossary_version integer,
    source_meta jsonb NOT NULL,
    status text DEFAULT 'ingested'::text NOT NULL,
    translated_by text,
    translation_ready boolean DEFAULT false NOT NULL,
    enrichment_attempts integer DEFAULT 0 NOT NULL,
    enrichment_retry_at timestamp with time zone,
    translation_warning_code text,
    translation_warning_count integer DEFAULT 0 NOT NULL,
    provider_retry_attempts integer DEFAULT 0 NOT NULL,
    provider_retry_at timestamp with time zone,
    provider_retry_category text,
    enrichment_discarded boolean DEFAULT false NOT NULL,
    facts_count integer,
    CONSTRAINT chapter_facts_count_check CHECK ((facts_count >= 0)),
    CONSTRAINT chapter_provider_retry_attempts_check CHECK ((provider_retry_attempts >= 0)),
    CONSTRAINT chapter_translation_warning_check CHECK ((((translation_warning_code IS NULL) AND (translation_warning_count = 0)) OR ((translation_warning_code = 'locked_terms_missing'::text) AND (translation_warning_count > 0))))
);

ALTER TABLE ONLY public.chapter FORCE ROW LEVEL SECURITY;


--
-- Name: chapter_fact; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chapter_fact (
    novel_id uuid NOT NULL,
    chapter_index integer NOT NULL,
    prompt_version text NOT NULL,
    ordinal integer NOT NULL,
    text text NOT NULL,
    source_hash text NOT NULL,
    requested_model text NOT NULL,
    served_provider text,
    served_model text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    category text,
    kind text,
    subjects uuid[] DEFAULT '{}'::uuid[] NOT NULL,
    CONSTRAINT chapter_fact_chapter_index_check CHECK ((chapter_index >= 0)),
    CONSTRAINT chapter_fact_ordinal_check CHECK ((ordinal >= 0)),
    CONSTRAINT chapter_fact_text_check CHECK ((btrim(text) <> ''::text))
);

ALTER TABLE ONLY public.chapter_fact FORCE ROW LEVEL SECURITY;


--
-- Name: chapter_failure; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chapter_failure (
    id bigint NOT NULL,
    novel_id uuid NOT NULL,
    chapter_index integer NOT NULL,
    stage text NOT NULL,
    error_type text NOT NULL,
    error_code text NOT NULL,
    occurred_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.chapter_failure FORCE ROW LEVEL SECURITY;


--
-- Name: chapter_failure_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.chapter_failure_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: chapter_failure_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.chapter_failure_id_seq OWNED BY public.chapter_failure.id;


--
-- Name: chapter_translation_version; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chapter_translation_version (
    novel_id uuid NOT NULL,
    chapter_index integer NOT NULL,
    version integer NOT NULL,
    translated_uri text NOT NULL,
    translated_by text NOT NULL,
    glossary_version integer,
    translation_fingerprint text,
    reason text DEFAULT 'initial'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.chapter_translation_version FORCE ROW LEVEL SECURITY;


--
-- Name: character_name_checkpoint; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.character_name_checkpoint (
    novel_id uuid NOT NULL,
    chapter_index integer NOT NULL,
    request_key text NOT NULL,
    source_hash text NOT NULL,
    requested_provider text NOT NULL,
    requested_model text NOT NULL,
    served_provider text NOT NULL,
    served_model text NOT NULL,
    response text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT character_name_checkpoint_chapter_index_check CHECK ((chapter_index > 0))
);

ALTER TABLE ONLY public.character_name_checkpoint FORCE ROW LEVEL SECURITY;


--
-- Name: character_name_occurrence; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.character_name_occurrence (
    id uuid NOT NULL,
    novel_id uuid NOT NULL,
    chapter_index integer NOT NULL,
    source_hash text NOT NULL,
    source_term text NOT NULL,
    char_start integer NOT NULL,
    char_end integer NOT NULL,
    quote text NOT NULL,
    CONSTRAINT character_name_occurrence_char_start_check CHECK ((char_start >= 0)),
    CONSTRAINT character_name_occurrence_check CHECK ((char_end > char_start))
);

ALTER TABLE ONLY public.character_name_occurrence FORCE ROW LEVEL SECURITY;


--
-- Name: character_name_review; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.character_name_review (
    novel_id uuid NOT NULL,
    source_term text NOT NULL,
    first_seen_chapter integer NOT NULL,
    source_hash text NOT NULL,
    char_start integer NOT NULL,
    char_end integer NOT NULL,
    quote text NOT NULL,
    candidates jsonb DEFAULT '[]'::jsonb NOT NULL,
    reason text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    selected_target text,
    selection_source text,
    reviewed_by text,
    reviewed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    term_role text DEFAULT 'chinese_person'::text NOT NULL,
    rendering_method text DEFAULT 'pinyin'::text NOT NULL,
    CONSTRAINT character_name_review_char_start_check CHECK ((char_start >= 0)),
    CONSTRAINT character_name_review_check CHECK ((char_end > char_start)),
    CONSTRAINT character_name_review_check1 CHECK ((((status = 'pending'::text) AND (selected_target IS NULL)) OR ((status = 'approved'::text) AND (selected_target IS NOT NULL)))),
    CONSTRAINT character_name_review_rendering_method_check CHECK ((rendering_method = ANY (ARRAY['pinyin'::text, 'restored_name'::text, 'translated_title'::text, 'semantic_translation'::text]))),
    CONSTRAINT character_name_review_selection_source_check CHECK ((selection_source = ANY (ARRAY['deterministic'::text, 'offered'::text, 'override'::text]))),
    CONSTRAINT character_name_review_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'approved'::text]))),
    CONSTRAINT character_name_review_term_role_check CHECK ((term_role = ANY (ARRAY['chinese_person'::text, 'foreign_person'::text, 'personal_title'::text, 'semantic_term'::text])))
);

ALTER TABLE ONLY public.character_name_review FORCE ROW LEVEL SECURITY;


--
-- Name: chunk; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chunk (
    id bigint NOT NULL,
    novel_id uuid NOT NULL,
    chapter_index integer NOT NULL,
    text text NOT NULL,
    embedding public.vector(768),
    embedding_space text
);

ALTER TABLE ONLY public.chunk FORCE ROW LEVEL SECURITY;


--
-- Name: chunk_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.chunk_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: chunk_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.chunk_id_seq OWNED BY public.chunk.id;


--
-- Name: embedding_config; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.embedding_config (
    singleton boolean DEFAULT true NOT NULL,
    provider text NOT NULL,
    model text DEFAULT ''::text NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    account_id uuid DEFAULT public.current_account() NOT NULL,
    CONSTRAINT embedding_config_provider_check CHECK ((provider = ANY (ARRAY['auto'::text, 'disabled'::text, 'server'::text, 'gemini'::text, 'openrouter'::text]))),
    CONSTRAINT embedding_config_singleton_check CHECK (singleton)
);

ALTER TABLE ONLY public.embedding_config FORCE ROW LEVEL SECURITY;


--
-- Name: fact_retraction; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.fact_retraction (
    novel_id uuid NOT NULL,
    chapter_index integer NOT NULL,
    prompt_version text NOT NULL,
    ordinal integer NOT NULL,
    retracted_by text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT fact_retraction_retracted_by_check CHECK ((btrim(retracted_by) <> ''::text))
);

ALTER TABLE ONLY public.fact_retraction FORCE ROW LEVEL SECURITY;


--
-- Name: glossary; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.glossary (
    novel_id uuid NOT NULL,
    source_term text NOT NULL,
    target_term text NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    locked_at_chapter integer NOT NULL,
    deleted boolean DEFAULT false NOT NULL,
    constraint_class text DEFAULT 'semantic_term'::text NOT NULL,
    CONSTRAINT glossary_constraint_class_check CHECK ((constraint_class = ANY (ARRAY['semantic_term'::text, 'character_name'::text])))
);

ALTER TABLE ONLY public.glossary FORCE ROW LEVEL SECURITY;


--
-- Name: glossary_candidate; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.glossary_candidate (
    novel_id uuid NOT NULL,
    source_term text NOT NULL,
    target_term text NOT NULL,
    proposals integer DEFAULT 1 NOT NULL,
    first_seen_chapter integer NOT NULL,
    last_seen_chapter integer NOT NULL
);

ALTER TABLE ONLY public.glossary_candidate FORCE ROW LEVEL SECURITY;


--
-- Name: glossary_candidate_chapter; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.glossary_candidate_chapter (
    novel_id uuid NOT NULL,
    source_term text NOT NULL,
    target_term text NOT NULL,
    chapter_index integer NOT NULL
);

ALTER TABLE ONLY public.glossary_candidate_chapter FORCE ROW LEVEL SECURITY;


--
-- Name: glossary_changelog; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.glossary_changelog (
    id bigint NOT NULL,
    novel_id uuid NOT NULL,
    source_term text NOT NULL,
    old_target text,
    new_target text NOT NULL,
    changed_at_chapter integer NOT NULL,
    prev_hash text,
    row_hash text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    seq integer NOT NULL
);

ALTER TABLE ONLY public.glossary_changelog FORCE ROW LEVEL SECURITY;


--
-- Name: glossary_changelog_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.glossary_changelog_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: glossary_changelog_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.glossary_changelog_id_seq OWNED BY public.glossary_changelog.id;


--
-- Name: job; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    novel_id uuid NOT NULL,
    chapter_index integer NOT NULL,
    stage text NOT NULL,
    state text DEFAULT 'pending'::text NOT NULL,
    idempotency_key text NOT NULL,
    batch_id text,
    attempts integer DEFAULT 0 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT job_stage_check CHECK ((stage = ANY (ARRAY['translate'::text, 'character_names'::text, 'records'::text])))
);

ALTER TABLE ONLY public.job FORCE ROW LEVEL SECURITY;


--
-- Name: mention_span; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.mention_span (
    id bigint NOT NULL,
    novel_id uuid NOT NULL,
    chapter_index integer NOT NULL,
    char_start integer NOT NULL,
    char_end integer NOT NULL
);

ALTER TABLE ONLY public.mention_span FORCE ROW LEVEL SECURITY;


--
-- Name: mention_span_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.mention_span_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: mention_span_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.mention_span_id_seq OWNED BY public.mention_span.id;


--
-- Name: novel; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.novel (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    title text NOT NULL,
    source_lang text DEFAULT 'en'::text NOT NULL,
    target_lang text DEFAULT 'en'::text NOT NULL,
    genre text,
    ontology jsonb NOT NULL,
    url_template text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    translation_provider text,
    translate_lookahead integer DEFAULT 5 NOT NULL,
    ingest_lookahead integer DEFAULT 10 NOT NULL,
    owner_id uuid DEFAULT public.current_account() NOT NULL
);

ALTER TABLE ONLY public.novel FORCE ROW LEVEL SECURITY;


--
-- Name: COLUMN novel.translate_lookahead; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.novel.translate_lookahead IS 'How many chapters past the reader''s position to keep translated. 0 = on demand only.';


--
-- Name: COLUMN novel.ingest_lookahead; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.novel.ingest_lookahead IS 'Stop fetching once this many chapters are ingested beyond the reader''s position. 0 = unlimited.';


--
-- Name: novel_provider_config; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.novel_provider_config (
    novel_id uuid NOT NULL,
    provider text NOT NULL,
    model text,
    base_url text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    translate_model text,
    extract_model text,
    facts_model text,
    CONSTRAINT novel_provider_config_provider_check CHECK ((provider = ANY (ARRAY['anthropic'::text, 'custom'::text, 'deepseek'::text, 'gemini'::text, 'groq'::text, 'ollama'::text])))
);

ALTER TABLE ONLY public.novel_provider_config FORCE ROW LEVEL SECURITY;


--
-- Name: provider_credential; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.provider_credential (
    provider text NOT NULL,
    base_url text,
    api_key_cipher bytea,
    api_key_nonce bytea,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    account_id uuid DEFAULT public.current_account() NOT NULL,
    key_version integer DEFAULT 0 NOT NULL,
    CONSTRAINT provider_credential_check CHECK (((api_key_cipher IS NULL) = (api_key_nonce IS NULL))),
    CONSTRAINT provider_credential_provider_check CHECK ((provider = ANY (ARRAY['anthropic'::text, 'custom'::text, 'deepseek'::text, 'gemini'::text, 'groq'::text, 'ollama'::text, 'openrouter'::text])))
);

ALTER TABLE ONLY public.provider_credential FORCE ROW LEVEL SECURITY;


--
-- Name: reader_progress; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.reader_progress (
    reader_id text NOT NULL,
    novel_id uuid NOT NULL,
    current_chapter integer NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT reader_progress_current_chapter_check CHECK ((current_chapter >= 0)),
    CONSTRAINT reader_progress_reader_id_check CHECK (((length(btrim(reader_id)) >= 1) AND (length(btrim(reader_id)) <= 200)))
);

ALTER TABLE ONLY public.reader_progress FORCE ROW LEVEL SECURITY;


--
-- Name: scrape_job; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.scrape_job (
    id bigint NOT NULL,
    novel_id uuid NOT NULL,
    start_url text NOT NULL,
    mode text DEFAULT 'translate'::text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    chapters_fetched integer DEFAULT 0 NOT NULL,
    last_error text,
    cancel_requested boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    max_queue_depth integer,
    CONSTRAINT scrape_job_mode_check CHECK ((mode = ANY (ARRAY['bootstrap'::text, 'translate'::text]))),
    CONSTRAINT scrape_job_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'running'::text, 'done'::text, 'error'::text, 'cancelled'::text])))
);

ALTER TABLE ONLY public.scrape_job FORCE ROW LEVEL SECURITY;


--
-- Name: COLUMN scrape_job.max_queue_depth; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.scrape_job.max_queue_depth IS 'Pause fetching while jobs:pending is at/above this depth. NULL = service default; 0 = no limit.';


--
-- Name: scrape_job_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.scrape_job_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: scrape_job_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.scrape_job_id_seq OWNED BY public.scrape_job.id;


--
-- Name: subject; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.subject (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    novel_id uuid NOT NULL,
    source_term text NOT NULL,
    first_seen_chapter integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    kind text NOT NULL,
    CONSTRAINT character_first_seen_chapter_check CHECK ((first_seen_chapter >= 0)),
    CONSTRAINT character_source_term_check CHECK ((btrim(source_term) <> ''::text)),
    CONSTRAINT subject_kind_check CHECK ((kind = ANY (ARRAY['character'::text, 'organization'::text, 'place'::text, 'item'::text])))
);

ALTER TABLE ONLY public.subject FORCE ROW LEVEL SECURITY;


--
-- Name: term_rendering_occurrence; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.term_rendering_occurrence (
    novel_id uuid NOT NULL,
    chapter_index integer NOT NULL,
    char_start integer NOT NULL,
    char_end integer NOT NULL,
    source_term text NOT NULL,
    display_term text NOT NULL,
    method text NOT NULL,
    CONSTRAINT term_rendering_occurrence_char_start_check CHECK ((char_start >= 0)),
    CONSTRAINT term_rendering_occurrence_check CHECK ((char_end > char_start)),
    CONSTRAINT term_rendering_occurrence_check1 CHECK ((char_length(display_term) = (char_end - char_start))),
    CONSTRAINT term_rendering_occurrence_method_check CHECK ((method = ANY (ARRAY['glossary'::text, 'aligned'::text])))
);

ALTER TABLE ONLY public.term_rendering_occurrence FORCE ROW LEVEL SECURITY;


--
-- Name: wiki_page; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.wiki_page (
    novel_id uuid NOT NULL,
    subject text NOT NULL,
    chapter_index integer NOT NULL,
    prompt_version text NOT NULL,
    title text NOT NULL,
    body text NOT NULL,
    facts_used integer NOT NULL,
    served_model text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT wiki_page_body_check CHECK ((btrim(body) <> ''::text)),
    CONSTRAINT wiki_page_chapter_index_check CHECK ((chapter_index >= 1)),
    CONSTRAINT wiki_page_facts_used_check CHECK ((facts_used >= 0)),
    CONSTRAINT wiki_page_subject_check CHECK ((btrim(subject) <> ''::text)),
    CONSTRAINT wiki_page_title_check CHECK ((btrim(title) <> ''::text))
);

ALTER TABLE ONLY public.wiki_page FORCE ROW LEVEL SECURITY;


--
-- Name: chapter_failure id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chapter_failure ALTER COLUMN id SET DEFAULT nextval('public.chapter_failure_id_seq'::regclass);


--
-- Name: chunk id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chunk ALTER COLUMN id SET DEFAULT nextval('public.chunk_id_seq'::regclass);


--
-- Name: glossary_changelog id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.glossary_changelog ALTER COLUMN id SET DEFAULT nextval('public.glossary_changelog_id_seq'::regclass);


--
-- Name: mention_span id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mention_span ALTER COLUMN id SET DEFAULT nextval('public.mention_span_id_seq'::regclass);


--
-- Name: scrape_job id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scrape_job ALTER COLUMN id SET DEFAULT nextval('public.scrape_job_id_seq'::regclass);


--
-- Name: account_cleanup account_cleanup_novel_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account_cleanup
    ADD CONSTRAINT account_cleanup_novel_id_key UNIQUE (novel_id);


--
-- Name: account_cleanup account_cleanup_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account_cleanup
    ADD CONSTRAINT account_cleanup_pkey PRIMARY KEY (id);


--
-- Name: account account_email_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account
    ADD CONSTRAINT account_email_key UNIQUE (email);


--
-- Name: account account_google_subject_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account
    ADD CONSTRAINT account_google_subject_key UNIQUE (google_subject);


--
-- Name: account_invitation account_invitation_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account_invitation
    ADD CONSTRAINT account_invitation_pkey PRIMARY KEY (token_hash);


--
-- Name: account_oauth_state account_oauth_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account_oauth_state
    ADD CONSTRAINT account_oauth_state_pkey PRIMARY KEY (state_hash);


--
-- Name: account account_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account
    ADD CONSTRAINT account_pkey PRIMARY KEY (id);


--
-- Name: account_queue_control account_queue_control_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account_queue_control
    ADD CONSTRAINT account_queue_control_pkey PRIMARY KEY (account_id);


--
-- Name: account_session account_session_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account_session
    ADD CONSTRAINT account_session_pkey PRIMARY KEY (token_hash);


--
-- Name: account_usage account_usage_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account_usage
    ADD CONSTRAINT account_usage_pkey PRIMARY KEY (id);


--
-- Name: chapter_fact chapter_fact_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chapter_fact
    ADD CONSTRAINT chapter_fact_pkey PRIMARY KEY (novel_id, chapter_index, prompt_version, ordinal);


--
-- Name: chapter_failure chapter_failure_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chapter_failure
    ADD CONSTRAINT chapter_failure_pkey PRIMARY KEY (id);


--
-- Name: chapter chapter_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chapter
    ADD CONSTRAINT chapter_pkey PRIMARY KEY (novel_id, chapter_index);


--
-- Name: chapter_translation_version chapter_translation_version_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chapter_translation_version
    ADD CONSTRAINT chapter_translation_version_pkey PRIMARY KEY (novel_id, chapter_index, version);


--
-- Name: character_name_checkpoint character_name_checkpoint_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.character_name_checkpoint
    ADD CONSTRAINT character_name_checkpoint_pkey PRIMARY KEY (novel_id, chapter_index, request_key);


--
-- Name: character_name_occurrence character_name_occurrence_novel_id_chapter_index_source_has_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.character_name_occurrence
    ADD CONSTRAINT character_name_occurrence_novel_id_chapter_index_source_has_key UNIQUE (novel_id, chapter_index, source_hash, char_start, char_end);


--
-- Name: character_name_occurrence character_name_occurrence_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.character_name_occurrence
    ADD CONSTRAINT character_name_occurrence_pkey PRIMARY KEY (id);


--
-- Name: character_name_review character_name_review_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.character_name_review
    ADD CONSTRAINT character_name_review_pkey PRIMARY KEY (novel_id, source_term);


--
-- Name: subject character_novel_id_source_term_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subject
    ADD CONSTRAINT character_novel_id_source_term_key UNIQUE (novel_id, source_term);


--
-- Name: subject character_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subject
    ADD CONSTRAINT character_pkey PRIMARY KEY (id);


--
-- Name: chunk chunk_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chunk
    ADD CONSTRAINT chunk_pkey PRIMARY KEY (id);


--
-- Name: embedding_config embedding_config_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.embedding_config
    ADD CONSTRAINT embedding_config_pkey PRIMARY KEY (account_id);


--
-- Name: fact_retraction fact_retraction_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fact_retraction
    ADD CONSTRAINT fact_retraction_pkey PRIMARY KEY (novel_id, chapter_index, prompt_version, ordinal);


--
-- Name: glossary_candidate_chapter glossary_candidate_chapter_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.glossary_candidate_chapter
    ADD CONSTRAINT glossary_candidate_chapter_pkey PRIMARY KEY (novel_id, source_term, target_term, chapter_index);


--
-- Name: glossary_candidate glossary_candidate_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.glossary_candidate
    ADD CONSTRAINT glossary_candidate_pkey PRIMARY KEY (novel_id, source_term, target_term);


--
-- Name: glossary_changelog glossary_changelog_novel_seq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.glossary_changelog
    ADD CONSTRAINT glossary_changelog_novel_seq UNIQUE (novel_id, seq);


--
-- Name: glossary_changelog glossary_changelog_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.glossary_changelog
    ADD CONSTRAINT glossary_changelog_pkey PRIMARY KEY (id);


--
-- Name: glossary glossary_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.glossary
    ADD CONSTRAINT glossary_pkey PRIMARY KEY (novel_id, source_term);


--
-- Name: job job_chapter_work_unique; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job
    ADD CONSTRAINT job_chapter_work_unique UNIQUE (novel_id, chapter_index, stage, idempotency_key);


--
-- Name: job job_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job
    ADD CONSTRAINT job_pkey PRIMARY KEY (id);


--
-- Name: mention_span mention_span_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mention_span
    ADD CONSTRAINT mention_span_pkey PRIMARY KEY (id);


--
-- Name: novel novel_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.novel
    ADD CONSTRAINT novel_pkey PRIMARY KEY (id);


--
-- Name: novel_provider_config novel_provider_config_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.novel_provider_config
    ADD CONSTRAINT novel_provider_config_pkey PRIMARY KEY (novel_id);


--
-- Name: provider_credential provider_credential_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.provider_credential
    ADD CONSTRAINT provider_credential_pkey PRIMARY KEY (account_id, provider);


--
-- Name: reader_progress reader_progress_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.reader_progress
    ADD CONSTRAINT reader_progress_pkey PRIMARY KEY (reader_id, novel_id);


--
-- Name: scrape_job scrape_job_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scrape_job
    ADD CONSTRAINT scrape_job_pkey PRIMARY KEY (id);


--
-- Name: term_rendering_occurrence term_rendering_occurrence_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.term_rendering_occurrence
    ADD CONSTRAINT term_rendering_occurrence_pkey PRIMARY KEY (novel_id, chapter_index, char_start, char_end);


--
-- Name: wiki_page wiki_page_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wiki_page
    ADD CONSTRAINT wiki_page_pkey PRIMARY KEY (novel_id, prompt_version, subject, chapter_index);


--
-- Name: account_session_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX account_session_owner ON public.account_session USING btree (account_id);


--
-- Name: account_usage_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX account_usage_owner ON public.account_usage USING btree (account_id, created_at);


--
-- Name: chapter_enrichment_retry; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chapter_enrichment_retry ON public.chapter USING btree (enrichment_retry_at) WHERE (status = ANY (ARRAY['error'::text, 'name_repair_error'::text, 'needs_name_review'::text]));


--
-- Name: chapter_fact_subjects; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chapter_fact_subjects ON public.chapter_fact USING gin (subjects);


--
-- Name: chapter_failure_lookup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chapter_failure_lookup ON public.chapter_failure USING btree (novel_id, chapter_index, occurred_at DESC);


--
-- Name: chapter_novel_raw_hash_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX chapter_novel_raw_hash_key ON public.chapter USING btree (novel_id, raw_hash);


--
-- Name: chapter_novel_source_url_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX chapter_novel_source_url_key ON public.chapter USING btree (novel_id, ((source_meta ->> 'source_url'::text))) WHERE (COALESCE((source_meta ->> 'source_url'::text), ''::text) <> ''::text);


--
-- Name: chapter_provider_retry_due; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chapter_provider_retry_due ON public.chapter USING btree (provider_retry_at) WHERE (provider_retry_at IS NOT NULL);


--
-- Name: character_name_occurrence_chapter; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX character_name_occurrence_chapter ON public.character_name_occurrence USING btree (novel_id, chapter_index);


--
-- Name: chunk_novel_id_chapter_index_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chunk_novel_id_chapter_index_idx ON public.chunk USING btree (novel_id, chapter_index);


--
-- Name: glossary_candidate_novel_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX glossary_candidate_novel_source ON public.glossary_candidate USING btree (novel_id, source_term);


--
-- Name: glossary_novel_target_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX glossary_novel_target_key ON public.glossary USING btree (novel_id, target_term) WHERE ((NOT deleted) AND (constraint_class = 'semantic_term'::text));


--
-- Name: mention_span_novel_id_chapter_index_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX mention_span_novel_id_chapter_index_idx ON public.mention_span USING btree (novel_id, chapter_index);


--
-- Name: novel_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX novel_owner ON public.novel USING btree (owner_id, id);


--
-- Name: scrape_job_novel_id_created_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX scrape_job_novel_id_created_at_idx ON public.scrape_job USING btree (novel_id, created_at DESC);


--
-- Name: scrape_job_one_active_per_novel; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX scrape_job_one_active_per_novel ON public.scrape_job USING btree (novel_id) WHERE (status = ANY (ARRAY['pending'::text, 'running'::text]));


--
-- Name: chapter_fact chapter_fact_immutability; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER chapter_fact_immutability BEFORE UPDATE ON public.chapter_fact FOR EACH ROW EXECUTE FUNCTION public.guard_chapter_fact_immutability();


--
-- Name: novel novel_cleanup; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER novel_cleanup BEFORE DELETE ON public.novel FOR EACH ROW EXECUTE FUNCTION public.enqueue_novel_cleanup();


--
-- Name: wiki_page wiki_page_immutability; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER wiki_page_immutability BEFORE UPDATE ON public.wiki_page FOR EACH ROW EXECUTE FUNCTION public.guard_wiki_page_immutability();


--
-- Name: account_invitation account_invitation_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account_invitation
    ADD CONSTRAINT account_invitation_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.account(id) ON DELETE CASCADE;


--
-- Name: account_queue_control account_queue_control_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account_queue_control
    ADD CONSTRAINT account_queue_control_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.account(id) ON DELETE CASCADE;


--
-- Name: account_queue_control account_queue_control_focus_novel_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account_queue_control
    ADD CONSTRAINT account_queue_control_focus_novel_id_fkey FOREIGN KEY (focus_novel_id) REFERENCES public.novel(id) ON DELETE SET NULL;


--
-- Name: account_session account_session_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account_session
    ADD CONSTRAINT account_session_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.account(id) ON DELETE CASCADE;


--
-- Name: account_usage account_usage_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account_usage
    ADD CONSTRAINT account_usage_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.account(id) ON DELETE CASCADE;


--
-- Name: chapter_fact chapter_fact_novel_id_chapter_index_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chapter_fact
    ADD CONSTRAINT chapter_fact_novel_id_chapter_index_fkey FOREIGN KEY (novel_id, chapter_index) REFERENCES public.chapter(novel_id, chapter_index) ON DELETE CASCADE;


--
-- Name: chapter_failure chapter_failure_novel_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chapter_failure
    ADD CONSTRAINT chapter_failure_novel_id_fkey FOREIGN KEY (novel_id) REFERENCES public.novel(id) ON DELETE CASCADE;


--
-- Name: chapter chapter_novel_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chapter
    ADD CONSTRAINT chapter_novel_id_fkey FOREIGN KEY (novel_id) REFERENCES public.novel(id) ON DELETE CASCADE;


--
-- Name: chapter_translation_version chapter_translation_version_novel_id_chapter_index_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chapter_translation_version
    ADD CONSTRAINT chapter_translation_version_novel_id_chapter_index_fkey FOREIGN KEY (novel_id, chapter_index) REFERENCES public.chapter(novel_id, chapter_index) ON DELETE CASCADE;


--
-- Name: character_name_checkpoint character_name_checkpoint_novel_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.character_name_checkpoint
    ADD CONSTRAINT character_name_checkpoint_novel_id_fkey FOREIGN KEY (novel_id) REFERENCES public.novel(id) ON DELETE CASCADE;


--
-- Name: character_name_occurrence character_name_occurrence_novel_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.character_name_occurrence
    ADD CONSTRAINT character_name_occurrence_novel_id_fkey FOREIGN KEY (novel_id) REFERENCES public.novel(id) ON DELETE CASCADE;


--
-- Name: character_name_occurrence character_name_occurrence_novel_id_source_term_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.character_name_occurrence
    ADD CONSTRAINT character_name_occurrence_novel_id_source_term_fkey FOREIGN KEY (novel_id, source_term) REFERENCES public.character_name_review(novel_id, source_term) ON DELETE CASCADE;


--
-- Name: character_name_review character_name_review_novel_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.character_name_review
    ADD CONSTRAINT character_name_review_novel_id_fkey FOREIGN KEY (novel_id) REFERENCES public.novel(id) ON DELETE CASCADE;


--
-- Name: subject character_novel_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subject
    ADD CONSTRAINT character_novel_id_fkey FOREIGN KEY (novel_id) REFERENCES public.novel(id) ON DELETE CASCADE;


--
-- Name: chunk chunk_novel_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chunk
    ADD CONSTRAINT chunk_novel_id_fkey FOREIGN KEY (novel_id) REFERENCES public.novel(id) ON DELETE CASCADE;


--
-- Name: embedding_config embedding_config_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.embedding_config
    ADD CONSTRAINT embedding_config_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.account(id) ON DELETE CASCADE;


--
-- Name: fact_retraction fact_retraction_novel_id_chapter_index_prompt_version_ordi_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fact_retraction
    ADD CONSTRAINT fact_retraction_novel_id_chapter_index_prompt_version_ordi_fkey FOREIGN KEY (novel_id, chapter_index, prompt_version, ordinal) REFERENCES public.chapter_fact(novel_id, chapter_index, prompt_version, ordinal) ON DELETE CASCADE;


--
-- Name: glossary_candidate_chapter glossary_candidate_chapter_novel_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.glossary_candidate_chapter
    ADD CONSTRAINT glossary_candidate_chapter_novel_id_fkey FOREIGN KEY (novel_id) REFERENCES public.novel(id) ON DELETE CASCADE;


--
-- Name: glossary_candidate glossary_candidate_novel_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.glossary_candidate
    ADD CONSTRAINT glossary_candidate_novel_id_fkey FOREIGN KEY (novel_id) REFERENCES public.novel(id) ON DELETE CASCADE;


--
-- Name: glossary_changelog glossary_changelog_novel_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.glossary_changelog
    ADD CONSTRAINT glossary_changelog_novel_id_fkey FOREIGN KEY (novel_id) REFERENCES public.novel(id) ON DELETE CASCADE;


--
-- Name: glossary glossary_novel_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.glossary
    ADD CONSTRAINT glossary_novel_id_fkey FOREIGN KEY (novel_id) REFERENCES public.novel(id) ON DELETE CASCADE;


--
-- Name: job job_novel_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job
    ADD CONSTRAINT job_novel_id_fkey FOREIGN KEY (novel_id) REFERENCES public.novel(id) ON DELETE CASCADE;


--
-- Name: mention_span mention_span_novel_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mention_span
    ADD CONSTRAINT mention_span_novel_id_fkey FOREIGN KEY (novel_id) REFERENCES public.novel(id) ON DELETE CASCADE;


--
-- Name: novel novel_owner_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.novel
    ADD CONSTRAINT novel_owner_id_fkey FOREIGN KEY (owner_id) REFERENCES public.account(id);


--
-- Name: novel_provider_config novel_provider_config_novel_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.novel_provider_config
    ADD CONSTRAINT novel_provider_config_novel_id_fkey FOREIGN KEY (novel_id) REFERENCES public.novel(id) ON DELETE CASCADE;


--
-- Name: provider_credential provider_credential_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.provider_credential
    ADD CONSTRAINT provider_credential_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.account(id) ON DELETE CASCADE;


--
-- Name: reader_progress reader_progress_novel_id_current_chapter_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.reader_progress
    ADD CONSTRAINT reader_progress_novel_id_current_chapter_fkey FOREIGN KEY (novel_id, current_chapter) REFERENCES public.chapter(novel_id, chapter_index) ON DELETE CASCADE;


--
-- Name: scrape_job scrape_job_novel_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scrape_job
    ADD CONSTRAINT scrape_job_novel_id_fkey FOREIGN KEY (novel_id) REFERENCES public.novel(id) ON DELETE CASCADE;


--
-- Name: term_rendering_occurrence term_rendering_occurrence_novel_id_chapter_index_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.term_rendering_occurrence
    ADD CONSTRAINT term_rendering_occurrence_novel_id_chapter_index_fkey FOREIGN KEY (novel_id, chapter_index) REFERENCES public.chapter(novel_id, chapter_index) ON DELETE CASCADE;


--
-- Name: wiki_page wiki_page_novel_id_chapter_index_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wiki_page
    ADD CONSTRAINT wiki_page_novel_id_chapter_index_fkey FOREIGN KEY (novel_id, chapter_index) REFERENCES public.chapter(novel_id, chapter_index) ON DELETE CASCADE;


--
-- Name: account; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.account ENABLE ROW LEVEL SECURITY;

--
-- Name: chapter account_boundary; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY account_boundary ON public.chapter AS RESTRICTIVE TO rls_reader, reader_progress_writer, ingest_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: chapter_fact account_boundary; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY account_boundary ON public.chapter_fact AS RESTRICTIVE TO rls_reader, reader_progress_writer, ingest_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: chapter_failure account_boundary; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY account_boundary ON public.chapter_failure AS RESTRICTIVE TO rls_reader, reader_progress_writer, ingest_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: chapter_translation_version account_boundary; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY account_boundary ON public.chapter_translation_version AS RESTRICTIVE TO rls_reader, reader_progress_writer, ingest_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: character_name_checkpoint account_boundary; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY account_boundary ON public.character_name_checkpoint AS RESTRICTIVE TO rls_reader, reader_progress_writer, ingest_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: character_name_occurrence account_boundary; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY account_boundary ON public.character_name_occurrence AS RESTRICTIVE TO rls_reader, reader_progress_writer, ingest_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: character_name_review account_boundary; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY account_boundary ON public.character_name_review AS RESTRICTIVE TO rls_reader, reader_progress_writer, ingest_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: chunk account_boundary; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY account_boundary ON public.chunk AS RESTRICTIVE TO rls_reader, reader_progress_writer, ingest_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: fact_retraction account_boundary; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY account_boundary ON public.fact_retraction AS RESTRICTIVE TO rls_reader, reader_progress_writer, ingest_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: glossary account_boundary; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY account_boundary ON public.glossary AS RESTRICTIVE TO rls_reader, reader_progress_writer, ingest_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: glossary_candidate account_boundary; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY account_boundary ON public.glossary_candidate AS RESTRICTIVE TO rls_reader, reader_progress_writer, ingest_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: glossary_candidate_chapter account_boundary; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY account_boundary ON public.glossary_candidate_chapter AS RESTRICTIVE TO rls_reader, reader_progress_writer, ingest_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: glossary_changelog account_boundary; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY account_boundary ON public.glossary_changelog AS RESTRICTIVE TO rls_reader, reader_progress_writer, ingest_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: job account_boundary; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY account_boundary ON public.job AS RESTRICTIVE TO rls_reader, reader_progress_writer, ingest_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: mention_span account_boundary; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY account_boundary ON public.mention_span AS RESTRICTIVE TO rls_reader, reader_progress_writer, ingest_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: novel_provider_config account_boundary; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY account_boundary ON public.novel_provider_config AS RESTRICTIVE TO rls_reader, reader_progress_writer, ingest_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: reader_progress account_boundary; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY account_boundary ON public.reader_progress AS RESTRICTIVE TO rls_reader, reader_progress_writer, ingest_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: scrape_job account_boundary; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY account_boundary ON public.scrape_job AS RESTRICTIVE TO rls_reader, reader_progress_writer, ingest_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: subject account_boundary; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY account_boundary ON public.subject AS RESTRICTIVE TO rls_reader, reader_progress_writer, ingest_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: term_rendering_occurrence account_boundary; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY account_boundary ON public.term_rendering_occurrence AS RESTRICTIVE TO rls_reader, reader_progress_writer, ingest_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: wiki_page account_boundary; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY account_boundary ON public.wiki_page AS RESTRICTIVE TO rls_reader, reader_progress_writer, ingest_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: account_cleanup; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.account_cleanup ENABLE ROW LEVEL SECURITY;

--
-- Name: account_queue_control; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.account_queue_control ENABLE ROW LEVEL SECURITY;

--
-- Name: account_usage; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.account_usage ENABLE ROW LEVEL SECURITY;

--
-- Name: account auth_identity; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY auth_identity ON public.account TO book_auth USING (true) WITH CHECK (true);


--
-- Name: chapter; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.chapter ENABLE ROW LEVEL SECURITY;

--
-- Name: chapter_fact; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.chapter_fact ENABLE ROW LEVEL SECURITY;

--
-- Name: chapter_fact chapter_fact_writer; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY chapter_fact_writer ON public.chapter_fact TO ingest_writer USING (true) WITH CHECK (true);


--
-- Name: chapter_failure; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.chapter_failure ENABLE ROW LEVEL SECURITY;

--
-- Name: chapter_translation_version; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.chapter_translation_version ENABLE ROW LEVEL SECURITY;

--
-- Name: character_name_checkpoint; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.character_name_checkpoint ENABLE ROW LEVEL SECURITY;

--
-- Name: character_name_checkpoint character_name_checkpoint_writer; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY character_name_checkpoint_writer ON public.character_name_checkpoint TO ingest_writer USING (true) WITH CHECK (true);


--
-- Name: character_name_occurrence; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.character_name_occurrence ENABLE ROW LEVEL SECURITY;

--
-- Name: character_name_review; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.character_name_review ENABLE ROW LEVEL SECURITY;

--
-- Name: chunk; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.chunk ENABLE ROW LEVEL SECURITY;

--
-- Name: account cleanup_identity; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY cleanup_identity ON public.account TO book_cleanup USING (true);


--
-- Name: novel cleanup_novel; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY cleanup_novel ON public.novel TO book_cleanup USING (true);


--
-- Name: account_cleanup cleanup_outbox; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY cleanup_outbox ON public.account_cleanup TO book_cleanup USING (true) WITH CHECK (true);


--
-- Name: account dispatch_accounts; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY dispatch_accounts ON public.account FOR SELECT TO book_dispatcher USING (true);


--
-- Name: chapter dispatch_chapter; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY dispatch_chapter ON public.chapter FOR SELECT TO book_dispatcher USING (true);


--
-- Name: account_queue_control dispatch_control; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY dispatch_control ON public.account_queue_control FOR SELECT TO book_dispatcher USING (true);


--
-- Name: account_queue_control dispatch_controls; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY dispatch_controls ON public.account_queue_control FOR SELECT TO book_dispatcher USING (true);


--
-- Name: job dispatch_job; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY dispatch_job ON public.job FOR SELECT TO book_dispatcher USING (true);


--
-- Name: novel dispatch_novel; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY dispatch_novel ON public.novel FOR SELECT TO book_dispatcher USING (true);


--
-- Name: scrape_job dispatch_scrape; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY dispatch_scrape ON public.scrape_job FOR SELECT TO book_dispatcher USING (true);


--
-- Name: embedding_config; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.embedding_config ENABLE ROW LEVEL SECURITY;

--
-- Name: fact_retraction; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.fact_retraction ENABLE ROW LEVEL SECURITY;

--
-- Name: fact_retraction fact_retraction_writer; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY fact_retraction_writer ON public.fact_retraction TO ingest_writer USING (true) WITH CHECK (true);


--
-- Name: chapter_fact gate_chapter_fact; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY gate_chapter_fact ON public.chapter_fact FOR SELECT TO rls_reader USING (((novel_id = public.reader_novel()) AND (chapter_index <= public.reader_chapter())));


--
-- Name: chunk gate_chunk; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY gate_chunk ON public.chunk USING (((novel_id = ( SELECT public.reader_novel() AS reader_novel)) AND (chapter_index <= ( SELECT public.reader_chapter() AS reader_chapter))));


--
-- Name: fact_retraction gate_fact_retraction; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY gate_fact_retraction ON public.fact_retraction FOR SELECT TO rls_reader USING (((novel_id = public.reader_novel()) AND (chapter_index <= public.reader_chapter())));


--
-- Name: mention_span gate_mention_span; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY gate_mention_span ON public.mention_span USING (((novel_id = public.reader_novel()) AND (chapter_index <= public.reader_chapter())));


--
-- Name: subject gate_subject; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY gate_subject ON public.subject FOR SELECT TO rls_reader USING (((novel_id = public.reader_novel()) AND (first_seen_chapter <= public.reader_chapter())));


--
-- Name: term_rendering_occurrence gate_term_rendering_occurrence; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY gate_term_rendering_occurrence ON public.term_rendering_occurrence USING (((novel_id = public.reader_novel()) AND (chapter_index <= public.reader_chapter())));


--
-- Name: wiki_page gate_wiki_page; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY gate_wiki_page ON public.wiki_page FOR SELECT TO rls_reader USING (((novel_id = public.reader_novel()) AND (chapter_index <= public.reader_chapter())));


--
-- Name: glossary; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.glossary ENABLE ROW LEVEL SECURITY;

--
-- Name: glossary_candidate; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.glossary_candidate ENABLE ROW LEVEL SECURITY;

--
-- Name: glossary_candidate_chapter; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.glossary_candidate_chapter ENABLE ROW LEVEL SECURITY;

--
-- Name: glossary_changelog; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.glossary_changelog ENABLE ROW LEVEL SECURITY;

--
-- Name: glossary glossary_knowledge_boundary; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY glossary_knowledge_boundary ON public.glossary AS RESTRICTIVE FOR SELECT TO rls_reader USING (((novel_id = public.reader_novel()) AND (locked_at_chapter <= public.reader_chapter())));


--
-- Name: job; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.job ENABLE ROW LEVEL SECURITY;

--
-- Name: mention_span; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.mention_span ENABLE ROW LEVEL SECURITY;

--
-- Name: novel; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.novel ENABLE ROW LEVEL SECURITY;

--
-- Name: novel_provider_config; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.novel_provider_config ENABLE ROW LEVEL SECURITY;

--
-- Name: account_queue_control own_account; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY own_account ON public.account_queue_control TO rls_reader, reader_progress_writer, ingest_writer USING (((account_id = ( SELECT public.current_account() AS current_account)) AND ( SELECT public.account_active() AS account_active))) WITH CHECK (((account_id = ( SELECT public.current_account() AS current_account)) AND ( SELECT public.account_active() AS account_active)));


--
-- Name: account_usage own_account; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY own_account ON public.account_usage TO rls_reader, reader_progress_writer, ingest_writer USING (((account_id = ( SELECT public.current_account() AS current_account)) AND ( SELECT public.account_active() AS account_active))) WITH CHECK (((account_id = ( SELECT public.current_account() AS current_account)) AND ( SELECT public.account_active() AS account_active)));


--
-- Name: embedding_config own_account; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY own_account ON public.embedding_config TO rls_reader, reader_progress_writer, ingest_writer USING (((account_id = ( SELECT public.current_account() AS current_account)) AND ( SELECT public.account_active() AS account_active))) WITH CHECK (((account_id = ( SELECT public.current_account() AS current_account)) AND ( SELECT public.account_active() AS account_active)));


--
-- Name: provider_credential own_account; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY own_account ON public.provider_credential TO rls_reader, reader_progress_writer, ingest_writer USING (((account_id = ( SELECT public.current_account() AS current_account)) AND ( SELECT public.account_active() AS account_active))) WITH CHECK (((account_id = ( SELECT public.current_account() AS current_account)) AND ( SELECT public.account_active() AS account_active)));


--
-- Name: account_cleanup own_cleanup; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY own_cleanup ON public.account_cleanup TO ingest_writer USING ((account_id = public.current_account())) WITH CHECK ((account_id = public.current_account()));


--
-- Name: account own_identity; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY own_identity ON public.account FOR SELECT TO rls_reader, reader_progress_writer, ingest_writer USING ((id = ( SELECT public.current_account() AS current_account)));


--
-- Name: chapter owned_metadata_read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_metadata_read ON public.chapter FOR SELECT TO rls_reader USING (public.owns_novel(novel_id));


--
-- Name: chapter_failure owned_metadata_read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_metadata_read ON public.chapter_failure FOR SELECT TO rls_reader USING (public.owns_novel(novel_id));


--
-- Name: chapter_translation_version owned_metadata_read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_metadata_read ON public.chapter_translation_version FOR SELECT TO rls_reader USING (public.owns_novel(novel_id));


--
-- Name: character_name_checkpoint owned_metadata_read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_metadata_read ON public.character_name_checkpoint FOR SELECT TO rls_reader USING (public.owns_novel(novel_id));


--
-- Name: character_name_occurrence owned_metadata_read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_metadata_read ON public.character_name_occurrence FOR SELECT TO rls_reader USING (public.owns_novel(novel_id));


--
-- Name: character_name_review owned_metadata_read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_metadata_read ON public.character_name_review FOR SELECT TO rls_reader USING (public.owns_novel(novel_id));


--
-- Name: glossary owned_metadata_read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_metadata_read ON public.glossary FOR SELECT TO rls_reader USING (public.owns_novel(novel_id));


--
-- Name: glossary_candidate owned_metadata_read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_metadata_read ON public.glossary_candidate FOR SELECT TO rls_reader USING (public.owns_novel(novel_id));


--
-- Name: glossary_candidate_chapter owned_metadata_read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_metadata_read ON public.glossary_candidate_chapter FOR SELECT TO rls_reader USING (public.owns_novel(novel_id));


--
-- Name: glossary_changelog owned_metadata_read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_metadata_read ON public.glossary_changelog FOR SELECT TO rls_reader USING (public.owns_novel(novel_id));


--
-- Name: job owned_metadata_read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_metadata_read ON public.job FOR SELECT TO rls_reader USING (public.owns_novel(novel_id));


--
-- Name: novel_provider_config owned_metadata_read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_metadata_read ON public.novel_provider_config FOR SELECT TO rls_reader USING (public.owns_novel(novel_id));


--
-- Name: reader_progress owned_metadata_read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_metadata_read ON public.reader_progress FOR SELECT TO rls_reader USING (public.owns_novel(novel_id));


--
-- Name: scrape_job owned_metadata_read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_metadata_read ON public.scrape_job FOR SELECT TO rls_reader USING (public.owns_novel(novel_id));


--
-- Name: chapter owned_progress_access; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_progress_access ON public.chapter TO reader_progress_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: chapter_fact owned_progress_access; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_progress_access ON public.chapter_fact TO reader_progress_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: chapter_failure owned_progress_access; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_progress_access ON public.chapter_failure TO reader_progress_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: chapter_translation_version owned_progress_access; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_progress_access ON public.chapter_translation_version TO reader_progress_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: character_name_checkpoint owned_progress_access; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_progress_access ON public.character_name_checkpoint TO reader_progress_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: character_name_occurrence owned_progress_access; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_progress_access ON public.character_name_occurrence TO reader_progress_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: character_name_review owned_progress_access; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_progress_access ON public.character_name_review TO reader_progress_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: chunk owned_progress_access; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_progress_access ON public.chunk TO reader_progress_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: fact_retraction owned_progress_access; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_progress_access ON public.fact_retraction TO reader_progress_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: glossary owned_progress_access; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_progress_access ON public.glossary TO reader_progress_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: glossary_candidate owned_progress_access; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_progress_access ON public.glossary_candidate TO reader_progress_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: glossary_candidate_chapter owned_progress_access; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_progress_access ON public.glossary_candidate_chapter TO reader_progress_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: glossary_changelog owned_progress_access; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_progress_access ON public.glossary_changelog TO reader_progress_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: job owned_progress_access; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_progress_access ON public.job TO reader_progress_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: mention_span owned_progress_access; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_progress_access ON public.mention_span TO reader_progress_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: novel_provider_config owned_progress_access; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_progress_access ON public.novel_provider_config TO reader_progress_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: reader_progress owned_progress_access; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_progress_access ON public.reader_progress TO reader_progress_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: scrape_job owned_progress_access; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_progress_access ON public.scrape_job TO reader_progress_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: subject owned_progress_access; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_progress_access ON public.subject TO reader_progress_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: term_rendering_occurrence owned_progress_access; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_progress_access ON public.term_rendering_occurrence TO reader_progress_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: wiki_page owned_progress_access; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_progress_access ON public.wiki_page TO reader_progress_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: chapter owned_stage_write; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_stage_write ON public.chapter TO ingest_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: chapter_fact owned_stage_write; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_stage_write ON public.chapter_fact TO ingest_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: chapter_failure owned_stage_write; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_stage_write ON public.chapter_failure TO ingest_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: chapter_translation_version owned_stage_write; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_stage_write ON public.chapter_translation_version TO ingest_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: character_name_checkpoint owned_stage_write; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_stage_write ON public.character_name_checkpoint TO ingest_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: character_name_occurrence owned_stage_write; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_stage_write ON public.character_name_occurrence TO ingest_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: character_name_review owned_stage_write; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_stage_write ON public.character_name_review TO ingest_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: chunk owned_stage_write; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_stage_write ON public.chunk TO ingest_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: fact_retraction owned_stage_write; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_stage_write ON public.fact_retraction TO ingest_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: glossary owned_stage_write; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_stage_write ON public.glossary TO ingest_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: glossary_candidate owned_stage_write; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_stage_write ON public.glossary_candidate TO ingest_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: glossary_candidate_chapter owned_stage_write; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_stage_write ON public.glossary_candidate_chapter TO ingest_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: glossary_changelog owned_stage_write; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_stage_write ON public.glossary_changelog TO ingest_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: job owned_stage_write; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_stage_write ON public.job TO ingest_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: mention_span owned_stage_write; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_stage_write ON public.mention_span TO ingest_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: novel_provider_config owned_stage_write; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_stage_write ON public.novel_provider_config TO ingest_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: reader_progress owned_stage_write; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_stage_write ON public.reader_progress TO ingest_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: scrape_job owned_stage_write; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_stage_write ON public.scrape_job TO ingest_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: subject owned_stage_write; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_stage_write ON public.subject TO ingest_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: term_rendering_occurrence owned_stage_write; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_stage_write ON public.term_rendering_occurrence TO ingest_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: wiki_page owned_stage_write; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owned_stage_write ON public.wiki_page TO ingest_writer USING (public.owns_novel(novel_id)) WITH CHECK (public.owns_novel(novel_id));


--
-- Name: novel owner_novel; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY owner_novel ON public.novel TO rls_reader, reader_progress_writer, ingest_writer USING (((owner_id = ( SELECT public.current_account() AS current_account)) AND ( SELECT public.account_active() AS account_active))) WITH CHECK (((owner_id = ( SELECT public.current_account() AS current_account)) AND ( SELECT public.account_active() AS account_active)));


--
-- Name: provider_credential; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.provider_credential ENABLE ROW LEVEL SECURITY;

--
-- Name: reader_progress; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.reader_progress ENABLE ROW LEVEL SECURITY;

--
-- Name: scrape_job; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.scrape_job ENABLE ROW LEVEL SECURITY;

--
-- Name: subject; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.subject ENABLE ROW LEVEL SECURITY;

--
-- Name: subject subject_writer; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY subject_writer ON public.subject TO ingest_writer USING (true) WITH CHECK (true);


--
-- Name: term_rendering_occurrence; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.term_rendering_occurrence ENABLE ROW LEVEL SECURITY;

--
-- Name: wiki_page; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.wiki_page ENABLE ROW LEVEL SECURITY;

--
-- Name: wiki_page wiki_page_writer; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY wiki_page_writer ON public.wiki_page TO ingest_writer USING (true) WITH CHECK (true);


--
-- Name: SCHEMA public; Type: ACL; Schema: -; Owner: -
--

GRANT USAGE ON SCHEMA public TO rls_reader;
GRANT USAGE ON SCHEMA public TO reader_progress_writer;
GRANT USAGE ON SCHEMA public TO book_auth;
GRANT USAGE ON SCHEMA public TO book_dispatcher;
GRANT USAGE ON SCHEMA public TO book_cleanup;


--
-- Name: FUNCTION account_active(); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.account_active() FROM PUBLIC;
GRANT ALL ON FUNCTION public.account_active() TO ingest_writer;
GRANT ALL ON FUNCTION public.account_active() TO rls_reader;
GRANT ALL ON FUNCTION public.account_active() TO reader_progress_writer;
GRANT ALL ON FUNCTION public.account_active() TO book_dispatcher;


--
-- Name: FUNCTION guard_edge_supersession(); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.guard_edge_supersession() FROM PUBLIC;


--
-- Name: FUNCTION reader_chapter(); Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON FUNCTION public.reader_chapter() TO rls_reader;


--
-- Name: FUNCTION reader_novel(); Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON FUNCTION public.reader_novel() TO rls_reader;


--
-- Name: FUNCTION request_account_deletion(actor uuid); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.request_account_deletion(actor uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION public.request_account_deletion(actor uuid) TO book_auth;


--
-- Name: FUNCTION worker_catalog(); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.worker_catalog() FROM PUBLIC;
GRANT ALL ON FUNCTION public.worker_catalog() TO book_worker;


--
-- Name: FUNCTION worker_due_retries(generic_limit integer, provider_limit integer); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.worker_due_retries(generic_limit integer, provider_limit integer) FROM PUBLIC;
GRANT ALL ON FUNCTION public.worker_due_retries(generic_limit integer, provider_limit integer) TO book_worker;


--
-- Name: FUNCTION worker_novel_owner(requested uuid); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.worker_novel_owner(requested uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION public.worker_novel_owner(requested uuid) TO book_worker;


--
-- Name: FUNCTION worker_scrape_catalog(); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.worker_scrape_catalog() FROM PUBLIC;
GRANT ALL ON FUNCTION public.worker_scrape_catalog() TO book_worker;


--
-- Name: TABLE account; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.account TO book_auth;
GRANT SELECT ON TABLE public.account TO book_dispatcher;
GRANT SELECT ON TABLE public.account TO ingest_writer;
GRANT SELECT ON TABLE public.account TO rls_reader;
GRANT SELECT ON TABLE public.account TO reader_progress_writer;
GRANT SELECT,DELETE,UPDATE ON TABLE public.account TO book_cleanup;


--
-- Name: TABLE account_cleanup; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.account_cleanup TO ingest_writer;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.account_cleanup TO book_cleanup;


--
-- Name: SEQUENCE account_cleanup_id_seq; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,USAGE ON SEQUENCE public.account_cleanup_id_seq TO ingest_writer;
GRANT SELECT,USAGE ON SEQUENCE public.account_cleanup_id_seq TO book_cleanup;


--
-- Name: TABLE account_invitation; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.account_invitation TO book_auth;


--
-- Name: TABLE account_oauth_state; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.account_oauth_state TO book_auth;


--
-- Name: TABLE account_queue_control; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT ON TABLE public.account_queue_control TO book_dispatcher;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.account_queue_control TO ingest_writer;
GRANT SELECT ON TABLE public.account_queue_control TO rls_reader;


--
-- Name: TABLE account_session; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.account_session TO book_auth;


--
-- Name: TABLE account_usage; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT ON TABLE public.account_usage TO ingest_writer;
GRANT SELECT,INSERT ON TABLE public.account_usage TO rls_reader;


--
-- Name: SEQUENCE account_usage_id_seq; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,USAGE ON SEQUENCE public.account_usage_id_seq TO ingest_writer;
GRANT USAGE ON SEQUENCE public.account_usage_id_seq TO rls_reader;


--
-- Name: TABLE chapter; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT ON TABLE public.chapter TO reader_progress_writer;
GRANT SELECT ON TABLE public.chapter TO book_dispatcher;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.chapter TO ingest_writer;


--
-- Name: COLUMN chapter.novel_id; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT(novel_id) ON TABLE public.chapter TO rls_reader;


--
-- Name: COLUMN chapter.chapter_index; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT(chapter_index) ON TABLE public.chapter TO rls_reader;


--
-- Name: COLUMN chapter.translation_ready; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT(translation_ready) ON TABLE public.chapter TO rls_reader;


--
-- Name: COLUMN chapter.enrichment_attempts; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT(enrichment_attempts) ON TABLE public.chapter TO rls_reader;


--
-- Name: COLUMN chapter.enrichment_retry_at; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT(enrichment_retry_at) ON TABLE public.chapter TO rls_reader;


--
-- Name: COLUMN chapter.provider_retry_attempts; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT(provider_retry_attempts) ON TABLE public.chapter TO rls_reader;


--
-- Name: COLUMN chapter.provider_retry_at; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT(provider_retry_at) ON TABLE public.chapter TO rls_reader;


--
-- Name: COLUMN chapter.provider_retry_category; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT(provider_retry_category) ON TABLE public.chapter TO rls_reader;


--
-- Name: COLUMN chapter.enrichment_discarded; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT(enrichment_discarded) ON TABLE public.chapter TO rls_reader;


--
-- Name: COLUMN chapter.facts_count; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT(facts_count) ON TABLE public.chapter TO rls_reader;


--
-- Name: TABLE chapter_fact; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE ON TABLE public.chapter_fact TO ingest_writer;
GRANT SELECT ON TABLE public.chapter_fact TO rls_reader;


--
-- Name: TABLE chapter_failure; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE ON TABLE public.chapter_failure TO ingest_writer;


--
-- Name: COLUMN chapter_failure.id; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT(id) ON TABLE public.chapter_failure TO reader_progress_writer;
GRANT SELECT(id) ON TABLE public.chapter_failure TO rls_reader;


--
-- Name: COLUMN chapter_failure.novel_id; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT(novel_id) ON TABLE public.chapter_failure TO reader_progress_writer;
GRANT SELECT(novel_id) ON TABLE public.chapter_failure TO rls_reader;


--
-- Name: COLUMN chapter_failure.chapter_index; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT(chapter_index) ON TABLE public.chapter_failure TO reader_progress_writer;
GRANT SELECT(chapter_index) ON TABLE public.chapter_failure TO rls_reader;


--
-- Name: COLUMN chapter_failure.error_code; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT(error_code) ON TABLE public.chapter_failure TO reader_progress_writer;
GRANT SELECT(error_code) ON TABLE public.chapter_failure TO rls_reader;


--
-- Name: COLUMN chapter_failure.occurred_at; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT(occurred_at) ON TABLE public.chapter_failure TO reader_progress_writer;
GRANT SELECT(occurred_at) ON TABLE public.chapter_failure TO rls_reader;


--
-- Name: SEQUENCE chapter_failure_id_seq; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,USAGE ON SEQUENCE public.chapter_failure_id_seq TO ingest_writer;


--
-- Name: TABLE chapter_translation_version; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT ON TABLE public.chapter_translation_version TO rls_reader;
GRANT SELECT,INSERT,DELETE ON TABLE public.chapter_translation_version TO ingest_writer;


--
-- Name: TABLE character_name_checkpoint; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT ON TABLE public.character_name_checkpoint TO ingest_writer;


--
-- Name: TABLE character_name_occurrence; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT ON TABLE public.character_name_occurrence TO rls_reader;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.character_name_occurrence TO ingest_writer;


--
-- Name: TABLE character_name_review; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT ON TABLE public.character_name_review TO rls_reader;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.character_name_review TO ingest_writer;


--
-- Name: TABLE chunk; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT ON TABLE public.chunk TO rls_reader;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.chunk TO ingest_writer;


--
-- Name: COLUMN chunk.embedding_space; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT(embedding_space) ON TABLE public.chunk TO rls_reader;


--
-- Name: SEQUENCE chunk_id_seq; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,USAGE ON SEQUENCE public.chunk_id_seq TO ingest_writer;


--
-- Name: TABLE embedding_config; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT ON TABLE public.embedding_config TO rls_reader;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.embedding_config TO ingest_writer;


--
-- Name: TABLE fact_retraction; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE ON TABLE public.fact_retraction TO ingest_writer;
GRANT SELECT ON TABLE public.fact_retraction TO rls_reader;


--
-- Name: TABLE glossary; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT ON TABLE public.glossary TO rls_reader;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.glossary TO ingest_writer;


--
-- Name: TABLE glossary_candidate; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT ON TABLE public.glossary_candidate TO rls_reader;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.glossary_candidate TO ingest_writer;


--
-- Name: TABLE glossary_candidate_chapter; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.glossary_candidate_chapter TO ingest_writer;


--
-- Name: TABLE glossary_changelog; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE ON TABLE public.glossary_changelog TO ingest_writer;


--
-- Name: SEQUENCE glossary_changelog_id_seq; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,USAGE ON SEQUENCE public.glossary_changelog_id_seq TO ingest_writer;


--
-- Name: TABLE job; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT ON TABLE public.job TO book_dispatcher;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.job TO ingest_writer;


--
-- Name: TABLE mention_span; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT ON TABLE public.mention_span TO rls_reader;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.mention_span TO ingest_writer;


--
-- Name: SEQUENCE mention_span_id_seq; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,USAGE ON SEQUENCE public.mention_span_id_seq TO ingest_writer;


--
-- Name: TABLE novel; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT ON TABLE public.novel TO rls_reader;
GRANT SELECT ON TABLE public.novel TO reader_progress_writer;
GRANT SELECT ON TABLE public.novel TO book_dispatcher;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.novel TO ingest_writer;
GRANT SELECT,DELETE ON TABLE public.novel TO book_cleanup;


--
-- Name: COLUMN novel.owner_id; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT(owner_id) ON TABLE public.novel TO rls_reader;
GRANT SELECT(owner_id) ON TABLE public.novel TO reader_progress_writer;


--
-- Name: TABLE novel_provider_config; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT ON TABLE public.novel_provider_config TO rls_reader;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.novel_provider_config TO ingest_writer;


--
-- Name: TABLE provider_credential; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT ON TABLE public.provider_credential TO rls_reader;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.provider_credential TO ingest_writer;


--
-- Name: TABLE reader_progress; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,UPDATE ON TABLE public.reader_progress TO reader_progress_writer;
GRANT SELECT ON TABLE public.reader_progress TO ingest_writer;


--
-- Name: TABLE scrape_job; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT ON TABLE public.scrape_job TO rls_reader;
GRANT SELECT,INSERT ON TABLE public.scrape_job TO reader_progress_writer;
GRANT SELECT ON TABLE public.scrape_job TO book_dispatcher;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.scrape_job TO ingest_writer;


--
-- Name: COLUMN scrape_job.cancel_requested; Type: ACL; Schema: public; Owner: -
--

GRANT UPDATE(cancel_requested) ON TABLE public.scrape_job TO reader_progress_writer;


--
-- Name: SEQUENCE scrape_job_id_seq; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,USAGE ON SEQUENCE public.scrape_job_id_seq TO reader_progress_writer;
GRANT SELECT,USAGE ON SEQUENCE public.scrape_job_id_seq TO ingest_writer;


--
-- Name: TABLE subject; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE ON TABLE public.subject TO ingest_writer;
GRANT SELECT ON TABLE public.subject TO rls_reader;


--
-- Name: TABLE term_rendering_occurrence; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT ON TABLE public.term_rendering_occurrence TO rls_reader;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.term_rendering_occurrence TO ingest_writer;


--
-- Name: TABLE wiki_page; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE ON TABLE public.wiki_page TO ingest_writer;
GRANT SELECT ON TABLE public.wiki_page TO rls_reader;


--
-- PostgreSQL database dump complete
--


-- SECURITY DEFINER ownership is explicit even when restoring with --no-owner.
GRANT CREATE ON SCHEMA public TO book_dispatcher,book_cleanup;
ALTER FUNCTION public.worker_novel_owner(uuid) OWNER TO book_dispatcher;
ALTER FUNCTION public.worker_catalog() OWNER TO book_dispatcher;
ALTER FUNCTION public.worker_due_retries(integer,integer) OWNER TO book_dispatcher;
ALTER FUNCTION public.worker_scrape_catalog() OWNER TO book_dispatcher;
ALTER FUNCTION public.request_account_deletion(uuid) OWNER TO book_cleanup;
REVOKE CREATE ON SCHEMA public FROM book_dispatcher,book_cleanup;
