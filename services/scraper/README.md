# scraper (Go)

Walks a novel's "next chapter" links and feeds `ingest-api`'s existing paste endpoint, so
a novel can be ingested from a URL instead of pasted chapter by chapter (PLAN.md Phase
N5). A long-running service, not a CLI — the web UI starts a scrape and polls its status.

## Supported sites

Per-site adapters, not a generic extractor (`adapter.go`'s `siteFor`, keyed by host):

- **freewebnovel.com** — an already-translated site. `mode=bootstrap`: fetched text has
  no separate original, so it's stored as both `raw_text` and `translated_text`
  (`TranslateStage`'s early-out then skips the LLM call for that chapter entirely).
- **look.twword.com** — a raw source-language site. `mode=translate`: the pipeline
  machine-translates it normally. **Its `robots.txt` disallows all bots except a named
  allowlist of major crawlers** — this scraper honors that (fails the job with a clear
  `last_error` rather than fetching anyway), so scraping this specific site will not
  currently succeed unless that changes.

Adding a third site is adding one file implementing the `Site` interface plus a
`siteFor` case — nothing else changes.

## Run it

```bash
# from repo root — needs postgres, redis, and ingest-api running
cd services/scraper
go run .
```

Config (`config.go`, env-driven with compose-friendly defaults): `DATABASE_URL`,
`REDIS_URL`, `INGEST_API_URL`, `SCRAPE_RATE_PER_SEC`, `SCRAPE_JITTER_MS`,
`SCRAPE_USER_AGENT`, `CONTENT_LEN_FLOOR`.

## Triggering a scrape

```bash
curl -X POST localhost:8081/novels/<novel-id>/scrape \
  -d '{"start_url":"https://freewebnovel.com/novel/x/chapter-1","mode":"bootstrap"}'
# -> {"id": <job id>}

curl localhost:8081/novels/<novel-id>/scrape/status
curl -X POST localhost:8081/novels/<novel-id>/scrape/cancel
```

Only one active job (`status` in `pending`/`running`) is allowed per novel at a time
(migration `0009`'s partial unique index) — chapter numbering continuity
(`chapter_index` assigned as `MAX(chapter_index)+1` at job start) depends on that.
Running two sources for one novel (e.g. a translated site, then a raw site to continue
past where it stops) means starting the second job only after the first reaches a
terminal status.

## Politeness

`politeness.go`: a token-bucket rate limiter + jitter + an honest `User-Agent` + a
`robots.txt` check (honors `Disallow` for `User-agent: *` only) wrap every request. A
`robots.txt` fetch failure fails open (many sites have none); an actual `Disallow` match
fails the request closed.
