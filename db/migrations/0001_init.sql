-- 0001_init.sql — core schema (authoritative DDL from instructions.md §4).
-- Forward-only. Everything is chapter-indexed and append-only (§0.2).
BEGIN;

CREATE EXTENSION IF NOT EXISTS vector;

-- ---------- Novels & chapters ----------
CREATE TABLE novel (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  title        TEXT NOT NULL,
  source_lang  TEXT NOT NULL DEFAULT 'en',
  target_lang  TEXT NOT NULL DEFAULT 'en',  -- translate stage runs only if source != target
  genre        TEXT,                         -- selects an ontology preset; NULL -> auto-induce
  ontology     JSONB NOT NULL,               -- ACTIVE ontology for this novel (§4.1)
  url_template TEXT,                          -- e.g. https://site/n/chapter-{n}; NULL if paste-only
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE chapter (
  novel_id       UUID NOT NULL REFERENCES novel(id),
  chapter_index  INT  NOT NULL,           -- internal canonical index (the gate key)
  raw_hash       TEXT NOT NULL,           -- sha256 of source body
  raw_uri        TEXT NOT NULL,           -- object-store key, source body
  translated_uri TEXT,                    -- object-store key, target body (NULL if no translation)
  glossary_version INT,                   -- glossary version used; NULL when source==target
  source_meta    JSONB NOT NULL,
  status         TEXT NOT NULL DEFAULT 'ingested', -- ingested|extracted|translated|done|error
  PRIMARY KEY (novel_id, chapter_index)
);

-- ---------- Entities, aliases, glossary ----------
CREATE TABLE entity (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  novel_id    UUID NOT NULL REFERENCES novel(id),
  kind        TEXT NOT NULL,              -- a kind defined in novel.ontology (not a fixed enum)
  canonical   TEXT NOT NULL,             -- canonical name (target lang)
  first_seen_chapter INT NOT NULL,
  embedding   VECTOR(1024)               -- pgvector: for retrieve-then-resolve
);
-- NOTE: the ivfflat ANN index on entity.embedding lives in 0003 and must be built
-- AFTER seed data exists (ivfflat trains on rows present at creation). See §4.

CREATE TABLE alias (
  entity_id   UUID NOT NULL REFERENCES entity(id),
  surface     TEXT NOT NULL,             -- a name/epithet in source OR target lang
  lang        TEXT NOT NULL,             -- BCP-47 code (zh, ja, en, ...)
  first_seen_chapter INT NOT NULL,       -- SPOILER-CRITICAL: gate aliases like facts (§4)
  PRIMARY KEY (entity_id, surface, lang)
);
CREATE INDEX ON alias (surface);

CREATE TABLE glossary (
  novel_id    UUID NOT NULL REFERENCES novel(id),
  source_term TEXT NOT NULL,             -- e.g. 青云宗
  target_term TEXT NOT NULL,             -- e.g. Azure Cloud Sect (LOCKED)
  entity_id   UUID REFERENCES entity(id),
  version     INT  NOT NULL DEFAULT 1,
  locked_at_chapter INT NOT NULL,
  PRIMARY KEY (novel_id, source_term)
);

-- Audit trail for retroactive glossary changes (tamper-evident, append-only).
CREATE TABLE glossary_changelog (
  id          BIGSERIAL PRIMARY KEY,
  novel_id    UUID NOT NULL,
  source_term TEXT NOT NULL,
  old_target  TEXT,
  new_target  TEXT NOT NULL,
  changed_at_chapter INT NOT NULL,
  prev_hash   TEXT,                       -- hash-chain over (prev_hash || row) for tamper evidence
  row_hash    TEXT NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------- Facts, edges (append-only, chapter-versioned) ----------
-- TWO time columns with DIFFERENT jobs:
--   source_chapter     = when the READER LEARNS it  -> the AUTHORIZATION key (gate/RLS)
--   valid_from_chapter = story-time it became true  -> display/ordering only
CREATE TABLE fact (
  id            BIGSERIAL PRIMARY KEY,
  novel_id      UUID NOT NULL REFERENCES novel(id),   -- needed for novel-scoped RLS
  entity_id     UUID NOT NULL REFERENCES entity(id),
  attribute     TEXT NOT NULL,           -- realm|status|title|location|...
  value         TEXT NOT NULL,
  valid_from_chapter INT NOT NULL,        -- story-time: when it became true
  source_chapter INT NOT NULL,            -- knowledge-time: chapter it was extracted from
  confidence    REAL NOT NULL DEFAULT 1.0
);
CREATE INDEX ON fact (entity_id, attribute, source_chapter, valid_from_chapter);

CREATE TABLE edge (
  id            BIGSERIAL PRIMARY KEY,
  novel_id      UUID NOT NULL REFERENCES novel(id),   -- needed for novel-scoped RLS
  src_id        UUID NOT NULL REFERENCES entity(id),
  dst_id        UUID NOT NULL REFERENCES entity(id),
  rel_type      TEXT NOT NULL,           -- teacher|enemy|ally|member_of|...
  valid_from_chapter INT NOT NULL,
  valid_to_chapter   INT,                -- null = still in effect
  source_chapter INT NOT NULL            -- knowledge-time: the authorization key
);
CREATE INDEX ON edge (src_id, source_chapter);

CREATE TABLE event (                      -- timeline rows
  id            BIGSERIAL PRIMARY KEY,
  novel_id      UUID NOT NULL REFERENCES novel(id),
  chapter_index INT NOT NULL,
  summary       TEXT NOT NULL,
  entity_ids    UUID[] NOT NULL
);
CREATE INDEX ON event (novel_id, chapter_index);

-- ---------- RAG chunks (chapter-filtered retrieval) ----------
CREATE TABLE chunk (
  id            BIGSERIAL PRIMARY KEY,
  novel_id      UUID NOT NULL REFERENCES novel(id),
  chapter_index INT NOT NULL,            -- the RAG gate key
  text          TEXT NOT NULL,           -- target-lang chunk (source text if untranslated)
  embedding     VECTOR(1024) NOT NULL    -- dimension MUST match EMBED_MODEL
);
CREATE INDEX ON chunk (novel_id, chapter_index);
-- Deliberately NO ANN index on chunk: exact scan scoped by (novel_id, chapter_index <= N)
-- is fast AND exactly correct. ANN post-filters, which can silently drop rows near the
-- chapter boundary — unacceptable when that filter is a security boundary. See §4.

-- ---------- Jobs & batches ----------
CREATE TABLE job (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  novel_id      UUID NOT NULL,
  chapter_index INT NOT NULL,
  stage         TEXT NOT NULL,           -- extract|resolve|translate|state
  state         TEXT NOT NULL DEFAULT 'pending', -- pending|batched|running|done|error|deadletter
  idempotency_key TEXT NOT NULL,         -- sha256 over EVERYTHING the output depends on (§4)
  batch_id      TEXT,                    -- provider batch id when submitted
  attempts      INT NOT NULL DEFAULT 0,
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (idempotency_key)               -- dedup: never run the same input twice
);

COMMIT;
