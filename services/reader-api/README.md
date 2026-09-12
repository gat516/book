# reader-api (Go)

Reader-facing spoiler gate for the novel engine (Phase 2). The service stores each
reader's maximum cleared chapter and exposes entity, wiki, timeline, and relationship
views that cannot read beyond that boundary.

## Security model

Every read is filtered twice:

1. The query explicitly applies the effective chapter (`min(?at, stored progress)`).
2. The same transaction sets `app.novel_id` and `app.current_chapter`; Postgres RLS then
   rejects future or cross-novel rows even if an application predicate is accidentally
   removed.

The reader and progress paths use separate pgx pools. Connections immediately switch to
the non-login group roles created by migrations: `rls_reader` can only read gated graph
tables, while `reader_progress_writer` can only inspect chapters and update progress.
Production login principals must be members of the corresponding role. No database
credentials are embedded in migrations.

`X-Reader-ID` is temporary development authentication. Responses are marked
`Cache-Control: private, no-store` so an intermediary cannot share one reader's gated
view with another.

## Run locally

```bash
docker compose -f deploy/docker-compose.yml up -d postgres
./db/migrate.sh

cd services/reader-api
go run .
```

The default address is `:8081`. Configuration:

- `READER_LISTEN_ADDR`
- `READER_DATABASE_URL` (falls back to `DATABASE_URL`)
- `PROGRESS_DATABASE_URL` (falls back to `DATABASE_URL`)
- `ASKAI_URL` (defaults to `http://localhost:8082`)
- `ASKAI_INTERNAL_TOKEN` (required shared bearer secret)
- `ASKAI_TIMEOUT_SECONDS` (defaults to `120`)
- `OBJECT_STORE_ENDPOINT`, `OBJECT_STORE_ACCESS_KEY`, `OBJECT_STORE_SECRET_KEY`,
  `OBJECT_STORE_BUCKET`, `OBJECT_STORE_USE_SSL` (same names/defaults as ingest-api —
  `GET /chapter` reads the chapter bodies pipeline already wrote)
- `INGEST_API_URL` (defaults to `http://localhost:8080`), `INGEST_INTERNAL_TOKEN`
  (shared secret reader-api sends to ingest-api's `POST /novels` — see ingest-api's README)

The compose owner can switch to both restricted roles for local development. Production
should use two distinct login credentials with only the required role membership.

## API

```bash
# Advance clearance. The chapter must exist and have status=done.
curl -X PUT localhost:8081/novels/<novel-id>/progress \
  -H 'X-Reader-ID: local-reader' \
  -d '{"chapter":5}'

curl localhost:8081/novels/<novel-id>/chapter/1 \
  -H 'X-Reader-ID: local-reader'

curl localhost:8081/novels/<novel-id>/wiki?at=3 \
  -H 'X-Reader-ID: local-reader'

curl localhost:8081/novels/<novel-id>/entity/<entity-id>?at=3 \
  -H 'X-Reader-ID: local-reader'

curl localhost:8081/novels/<novel-id>/timeline?at=3 \
  -H 'X-Reader-ID: local-reader'

curl localhost:8081/novels/<novel-id>/relationships/<entity-id>?at=3 \
  -H 'X-Reader-ID: local-reader'

curl -X POST localhost:8081/novels/<novel-id>/ask \
  -H 'X-Reader-ID: local-reader' \
  -d '{"question":"What did the protagonist learn?","at":3}'

# Novel list/detail are ungated (no X-Reader-ID needed) — novel metadata has no
# source_chapter to gate on.
curl localhost:8081/novels
curl localhost:8081/novels/<novel-id>

# Creation proxies to ingest-api (see ingest-api's README for the auth it requires there —
# reader-api forwards it, the browser never needs the token).
curl -X POST localhost:8081/novels -d '{"title":"Test Novel"}'

# Glossary: read is gated like every other view; correction proxies to ingest-api and
# requires X-Reader-ID (a correction is a specific reader's action, unlike novel/chapter
# creation, which have no reader-identity concept at all).
curl localhost:8081/novels/<novel-id>/glossary?at=3 \
  -H 'X-Reader-ID: local-reader'
curl -X PATCH localhost:8081/novels/<novel-id>/glossary/<url-encoded-source-term> \
  -H 'X-Reader-ID: local-reader' \
  -d '{"target_term":"Corrected Term","at_chapter":3}'
```

Omitting `at` uses stored progress. A larger value is capped to stored progress; a lower
value supports rereading without lowering the durable clearance ceiling.

`GET /chapter/{n}` has no `?at=` — chapter `n` *is* the resource, gated by `n <= stored
progress` (404 otherwise). Its response `at` field is always the stored progress, not
`n`; the web client uses that value as the entity-hover cache key for spans on that
chapter (see `services/web/`).

## Records status and maintenance

Knowledge is a **records generation**: one immutable extraction configuration per novel,
filled chapter by chapter. `novel.active_record_generation` says which one readers see.

```bash
# One chapter's records, plus the status envelope every records surface carries.
curl "localhost:8081/novels/<novel-id>/chapter/3/rows" -H 'X-Reader-ID: local-reader'

# What the deterministic checks rejected, and which names stayed unresolved.
curl "localhost:8081/novels/<novel-id>/chapter/3/records/inspector" -H 'X-Reader-ID: local-reader'
```

`status` reports `extraction_status` (`pending`, `processing`, `ready`, `failed`),
`rendering_status` (`pending`, `ready`, `failed`), a bounded `failure_detail`, a warning
count, and an opaque `version`. The version is derived only from runs at or below the
reader's own chapter, so publishing chapter 40 never invalidates a chapter-3 reader's
cache token.

Rendering is separate on purpose: a failed English rendering publishes the source records
with `render_status=failed` and the UI falls back to source-language values, rather than
discarding an extraction over a display problem.

Three maintenance actions replace the old repair panel. All proxy to ingest-api behind its
internal token, so the browser never holds it:

- `POST /novels/{id}/chapter/{n}/records/retry` — re-run a failed chapter. Published runs
  are untouched.
- `POST /novels/{id}/chapter/{n}/records/render-retry` — re-run only the English
  rendering; source records and evidence stay as they are.
- `POST /novels/{id}/records/rebuild` — open a new generation and re-enrich every saved
  chapter in order. Published extraction content is immutable, so a prompt, ontology or
  model change is a new generation rather than an edit. The new generation becomes active
  immediately and starts empty: readers see pending knowledge while it fills, instead of a
  mix of two generations' identity decisions.

Failure classes stay a bounded vocabulary: `pipeline/failures.py` classifies an exception
at the moment it is raised and stores only the class, so freeform provider text or source
prose never reaches a read path (migration 0046).

## Tests

```bash
go test ./...

# Run the Postgres/RLS suite against a migrated database:
READER_TEST_DATABASE_URL=postgres://engine:engine@localhost:5432/novel_engine \
  go test ./...
```

Without `READER_TEST_DATABASE_URL`, database-backed tests skip cleanly.

### Glossary management and pending navigation

The web glossary is available from the reader, chapter list, and pending view.
It supports create (bootstrap), read, edit, and confirmed deletion. DELETE is
proxied to ingest-api with the same `X-Reader-ID` requirement as corrections.
Before a reader has progress, GET glossary returns only chapter-zero seed terms;
otherwise the existing `locked_at_chapter <= min(progress, at)` gate remains.
Deleted terms are hidden. Changes affect future translation work, not stored prose.

`has_next` now means the next chapter exists, regardless of processing status.
Clicking Next checks its status: a finished chapter advances progress and opens;
an unfinished one opens its preview and requests priority. This does not advance
progress or unlock graph reads until the chapter finishes. The pending view has a
focused priority/retry button, and successful retry resumes status polling.

Glossary rows may include `entity_id` for the clickable reader inspector. This is
optional: unbound seeds omit it, and a seed linked to a future entity also omits it
until `entity.first_seen_chapter <= at`. A left join under the reader role preserves
the visible glossary seed without exposing a future entity identifier.

## Independent translation readiness

Migration 0021 adds `chapter.translation_ready`. Chapter access, progress and preview
readiness accept that flag independently of overall pipeline status. Chapter-list
`graph_status` is separate from the reader-facing status, so a chapter can be Ready
with graph enrichment pending or failed. Source-chapter RLS gates are unchanged.
