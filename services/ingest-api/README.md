# ingest-api (Go)

Paste-ingest entrypoint for the novel engine (see `instructions.md` §7.1). Accepts a
pasted chapter, stores the body in the object store, records a `chapter` row in Postgres,
and signals the offline pipeline via Redis. It is a **writer** service — no auth or
spoiler gate here (that lives in `reader-api`, §8).

## What it does per paste

1. `sha256(raw_text)` → `raw_hash` (dedup / cache key, §3.1).
2. Upload the body to the object store at `novels/{id}/chapters/{index}/raw.txt`.
3. Insert a `chapter` row (`ON CONFLICT DO NOTHING` → idempotent re-paste).
4. `LPUSH` a `{novel_id, chapter_index}` pointer onto the Redis `jobs:pending` list.

The pipeline (a later milestone) drains `jobs:pending` and loads the body from
`chapter.raw_uri`. This service deliberately does **not** manage the `job` table or
stage orchestration.

## Run it

```bash
# from repo root — bring up backing services
docker compose -f deploy/docker-compose.yml up -d postgres redis minio
# migrations must be applied (db/migrations) — see repo root

cd services/ingest-api
go run .          # listens on :8080, auto-creates the object-store bucket
```

Config is read from the environment with localhost defaults matching the compose stack
(`config.go`): `DATABASE_URL`, `REDIS_URL`, `OBJECT_STORE_ENDPOINT`,
`OBJECT_STORE_ACCESS_KEY/SECRET_KEY/BUCKET`, `LISTEN_ADDR`, `INGEST_INTERNAL_TOKEN`
(required — see below).

## Endpoints

`POST /novels` and `DELETE /novels/{id}` are the routes with any auth: they require
`Authorization: Bearer $INGEST_INTERNAL_TOKEN`, since `reader-api` is the only intended
caller (it proxies the novel lifecycle for the browser — see `services/reader-api/ingest.go`).
Every other route is unauthenticated, per this service's "no auth/gate here" design.

```bash
# register a novel (genre selects a preset ontology, §4.1; unknown/empty → generic)
curl -sX POST localhost:8080/novels \
  -H "Authorization: Bearer $INGEST_INTERNAL_TOKEN" \
  -d '{"title":"Test Novel","source_lang":"en","target_lang":"en","genre":"xianxia"}'
# → {"id":"<uuid>"}

# paste a chapter
curl -sX POST localhost:8080/novels/<uuid>/chapters \
  -d '{"chapter_index":1,"raw_text":"Once upon a time..."}'
# → 202 {"novel_id":...,"chapter_index":1,"raw_hash":"sha256:...","status":"ingested"}

# delete a novel and everything derived from it — chapters, graph, glossary, queued work
# and its object-store bodies. Irreversible; migration 0030 owns the cascade.
curl -sX DELETE localhost:8080/novels/<uuid> \
  -H "Authorization: Bearer $INGEST_INTERNAL_TOKEN"
# → {"deleted":true}   (404 {"error":"no such novel"} if it was already gone)

curl -s localhost:8080/healthz    # → {"status":"ok"} (pings pg + redis)

# correct a glossary term a human believes is wrong (PLAN.md Phase N2). Forward-only:
# chapters already translated with the old term are unaffected — see glossary.go.
curl -sX PATCH localhost:8080/novels/<uuid>/glossary/<url-encoded-source-term> \
  -d '{"target_term":"Corrected Term","at_chapter":42}'
# → {"novel_id":...,"source_term":...,"target_term":"Corrected Term","version":<N>}
```

## Verify the three landing zones

```bash
docker exec -i deploy-postgres-1 psql -U engine -d novel_engine \
  -c "SELECT novel_id, chapter_index, status, raw_uri FROM chapter;"
docker exec deploy-redis-1 redis-cli LRANGE jobs:pending 0 -1
docker exec deploy-minio-1 sh -c \
  "mc alias set local http://localhost:9000 minio minio12345 >/dev/null; \
   mc ls --recursive local/raw-chapters"
```

## Tests

```bash
go test ./...   # glossary_hash_test.go's cross-language guard runs standalone, no DB

# Run the Postgres-backed glossary correction tests against a migrated database:
INGEST_TEST_DATABASE_URL=postgres://engine:engine@localhost:5432/novel_engine \
  go test ./...
```

Without `INGEST_TEST_DATABASE_URL`, the database-backed tests skip cleanly.

## Files

| File | Role |
|---|---|
| `main.go` | wire config + clients, ensure bucket, register routes, serve |
| `config.go` | env → `Config` with compose defaults |
| `envelope.go` | `ChapterEnvelope` (§3.1) + Redis `QueueMessage` |
| `ontology.go` | genre → preset ontology (§4.1) + generic fallback |
| `store.go` | pg / redis / minio data-access helpers |
| `handlers.go` | HTTP handlers + validation |
| `glossary.go` | glossary correction — Go port of `resolve.py`'s hash-chain audit trail |

### Glossary deletion and reader priority

Migration `0019_glossary_deletion.sql` adds glossary tombstones. Apply it before
starting the updated APIs/pipeline. `DELETE /novels/{id}/glossary/{term}` accepts
`{"at_chapter": 12}`. It removes the term from active translation constraints,
advances the novel-wide version, and appends a hash-chained audit entry whose
`new_target` is `""` (the deletion marker). Existing translations and graph facts
are unchanged. RESOLVE will not promote a deleted source again; explicitly adding
it via `/glossary/bootstrap` restores it with a fresh version. Deleted targets
can be reused. Create/correct requests trim terms and reject blank values.

`POST /novels/{id}/translate-ahead` with
`{"from": 12, "count": 1, "priority": true}` retries an errored chapter or moves an
existing pending pointer to the worker's next dequeue position. It does not flush
other work or cancel the currently processing chapter. Repeated requests leave
one pending pointer; the Redis processing-list check and move are atomic. The
response's `prioritized` reports whether the pointer moved; completed or active
chapters are not duplicated by the promotion script. Ordinary lookahead requests
retain FIFO behavior.
