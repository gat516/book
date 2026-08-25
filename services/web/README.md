# web (TS/React)

The reader UI (Phase 5, PLAN.md), scoped to one page: chapter text with highlighted
mentions, a hover card, an Ask-AI box, and next/prev navigation that advances progress.
No state library — server state is the state.

## Run locally

```bash
# reader-api must be running (see services/reader-api/README.md) — the dev server
# proxies /api -> http://localhost:8081.
cd services/reader-api && ASKAI_INTERNAL_TOKEN=<token> go run .

cd services/web
npm install
npm run dev
```

Open `http://localhost:5173/?novel=<novel-id>` (or set `VITE_NOVEL_ID` in `.env.local`
as a default — there's no novel picker in Milestone 1). There's no login: a random
reader id is generated once and kept in `localStorage` (the accepted Phase-2 "fake
principal" tradeoff).

## Contract

`src/types.ts` mirrors `services/reader-api/models.go`'s JSON shapes by hand — this is a
two-service monorepo, not a shared-schema one. If a reader-api response shape changes,
update both.

## Checks

```bash
npx tsc -b   # typecheck
npm run build
```
