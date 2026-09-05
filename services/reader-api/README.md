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

## Knowledge repair status

`GET /novels/{id}/repair` reports whether this novel's knowledge is being withheld and
what, if anything, is replacing it. Ungated and safe for any reader: it returns counts,
states and revision identifiers, never quote text or claim values.

```bash
# Anyone may read it.
curl localhost:8081/novels/<novel-id>/repair -H 'X-Reader-ID: local-reader'

# An operator additionally presents the repair token; the response's `operator` field is
# the server's answer, and is what the UI keys its controls off.
curl localhost:8081/novels/<novel-id>/repair \
  -H 'X-Reader-ID: local-reader' \
  -H "X-Operator-Token: $READER_REPAIR_OPERATOR_TOKEN"
```

Two independent tracks, `graph` and `events`, because event extraction has its own
activation pointer (migration 0039) — one can be quarantined while the other is fine.
Each reports a `state` (`ready`, `quarantined`, `rebuilding`, `awaiting_review`, `failed`,
`unavailable`), a server-authored `reason` sentence clients should render rather than
re-derive, `withheld_claims`, the `chapters` counts for whichever revision is currently
doing work, the staging `replacement` if one exists, and a bounded `failures` ledger.

Two things about that ledger are load-bearing:

- `failures[].category` is a safe class and `detail` a fixed sentence. The stored
  exception text is freeform and can embed source prose or a connection string, so
  `pipeline/failures.py` classifies it at the moment of failure and stores only the class
  (migration 0046). The text never leaves the database, and Go only renders the class —
  `tests/test_repair.py` fails the build if a class Python emits has no sentence here.
  Migration 0020 kept such text out of the durable failure history for the same reason.
- `retryable` goes false once every failure has exhausted its attempts.
  `graph_rebuild.graph_retry_delay_minutes` returns `None` past attempt 3 and `retry_at`
  is never set again, so the chapter is silently abandoned. This field is how that
  otherwise-invisible dead end reaches a screen.

Repair reads and writes are **ungated** on this deployment. The panel shows unreviewed
claims and their source quotes from chapters ahead of the reader, and anyone who can reach
the page can start, activate or roll back a rebuild. That is a deliberate choice for a
single-operator install.

Two things still hold. The server-to-server bearer token to ingest-api is unchanged, so a
browser still cannot reach ingest-api directly. And `repair_preview` / `repair_progress` /
`repair_extraction` remain executable only by the `repair_operator` role, reached through
its own pool (`REPAIR_OPERATOR_DATABASE_URL`) — that keeps spoiler-bearing rows away from
`rls_reader`, which askai connects as, without asking anyone to sign in.

If this ever serves readers who are not the operator, the gate belongs on **reading
progress** — show a chapter's names once that chapter has been read — rather than on an
admin credential, which answers a different question than the one that matters.

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
