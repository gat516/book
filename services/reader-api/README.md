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
```

Omitting `at` uses stored progress. A larger value is capped to stored progress; a lower
value supports rereading without lowering the durable clearance ceiling.

`GET /chapter/{n}` has no `?at=` — chapter `n` *is* the resource, gated by `n <= stored
progress` (404 otherwise). Its response `at` field is always the stored progress, not
`n`; the web client uses that value as the entity-hover cache key for spans on that
chapter (see `services/web/`).

## Tests

```bash
go test ./...

# Run the Postgres/RLS suite against a migrated database:
READER_TEST_DATABASE_URL=postgres://engine:engine@localhost:5432/novel_engine \
  go test ./...
```

Without `READER_TEST_DATABASE_URL`, database-backed tests skip cleanly.
