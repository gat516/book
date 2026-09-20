# reader-api (Go)

Reader-facing spoiler gate for the novel engine (Phase 2). The service stores each
reader's maximum cleared chapter and exposes chapter, wiki, glossary, facts-status and
Ask AI views that cannot read beyond that boundary.

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

Hosted requests use invited Google sessions. The server discards browser-supplied
`X-Reader-ID`/`X-Account-ID`, checks ownership before any proxy/cache/object read, and sets
`app.account_id` on each pool checkout. Ownership RLS composes with the chapter gates.
`BOOK_MODE=local` assigns the fixed local owner with no login. Responses are marked
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

The compose owner can switch to both restricted roles for local development. The hosted
reader login can switch to the two data roles; its separate auth login can switch only
to `book_auth`. See [hosted setup](../../deploy/hosted/README.md).

## API

These examples use local mode. Hosted calls use the session cookie and, for mutations,
the server's CSRF token plus the configured Origin; the web client attaches them.

```bash
# Advance clearance. The chapter must exist and have status=done.
curl -X PUT localhost:8081/novels/<novel-id>/progress \
  -d '{"chapter":5}'

curl localhost:8081/novels/<novel-id>/chapter/1

# Wiki: characters the reader has met, and one character's page, from tagged facts.
curl localhost:8081/novels/<novel-id>/wiki/pages
curl localhost:8081/novels/<novel-id>/wiki/pages/<character-id>

curl -X POST localhost:8081/novels/<novel-id>/ask \
  -d '{"question":"What did the protagonist learn?","at":3}'

# Novel list/detail have no chapter gate, but always enforce account ownership.
curl localhost:8081/novels
curl localhost:8081/novels/<novel-id>

# Creation proxies to ingest-api (see ingest-api's README for the auth it requires there —
# reader-api forwards it, the browser never needs the token).
curl -X POST localhost:8081/novels -d '{"title":"Test Novel"}'

# Glossary: read is gated like every other view; correction proxies to ingest-api and
# uses the server-resolved account for authorization and audit.
curl localhost:8081/novels/<novel-id>/glossary?at=3
curl -X PATCH localhost:8081/novels/<novel-id>/glossary/<url-encoded-source-term> \
  -d '{"target_term":"Corrected Term","at_chapter":3}'
```

Omitting `at` uses stored progress. A larger value is capped to stored progress; a lower
value supports rereading without lowering the durable clearance ceiling.

`GET /chapter/{n}` has no `?at=` — chapter `n` *is* the resource, gated by `n <= stored
progress` (404 otherwise). Its response `at` field is always the stored progress, not
`n`; the web client uses that value as `at` for everything it does on that chapter
(see `services/web/`).

## Facts status and controls

FACTS writes a chapter's tagged facts in one call (see `services/pipeline/README.md`).

```bash
# One chapter's facts status: gated like every reader view (404 past stored progress).
curl "localhost:8081/novels/<novel-id>/chapter/3/facts/status"
```

`status.state` is `pending`, `processing` (a retry is scheduled), `failed` (retries
exhausted), or `ready` (the chapter's facts are written), with `facts_count`, `discarded`,
a bounded `failure_detail`, and durable retry metadata (`retry_attempts`,
`retry_max_attempts`, `retry_at`, `retry_category`). Never fact text.

The controls proxy to ingest-api behind its internal token, so the browser never holds it:

- `POST /novels/{id}/facts/extract` — queue every readable chapter that has no facts, in
  order. Chapters that already have facts are untouched.
- `POST /novels/{id}/facts/stop` — pause the book's unfinished facts work; finished
  chapters keep their facts, and translation is never cancelled.
- `POST /novels/{id}/chapter/{n}/facts/retry` / `.../discard` — re-run or pause one chapter.
- `GET /novels/{id}/facts/status` — book-wide counts (eligible/done/missing) and whether
  work is running. Metadata only, not spoiler-gated.

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
proxied to ingest-api with the same trusted account context as corrections.
Before a reader has progress, GET glossary returns only chapter-zero seed terms;
otherwise the existing `locked_at_chapter <= min(progress, at)` gate remains.
Deleted terms are hidden. Changes affect future translation work, not stored prose.

`has_next` now means the next chapter exists, regardless of processing status.
Clicking Next checks its status: a finished chapter advances progress and opens;
an unfinished one opens its preview and requests priority. This does not advance
progress or unlock graph reads until the chapter finishes. The pending view has a
focused priority/retry button, and successful retry resumes status polling.

## Independent translation readiness

Migration 0021 adds `chapter.translation_ready`. Chapter access, progress and preview
readiness accept that flag independently of overall pipeline status. Chapter-list
`graph_status` is separate from the reader-facing status, so a chapter can be Ready
with graph enrichment pending or failed. Source-chapter RLS gates are unchanged.
