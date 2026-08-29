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

### Clickable entity mode

Highlighted names are always buttons, even without recorded facts. **Reading settings
→ Show hover previews** adds previews on hover; turning it off keeps clicking available.
The setting is saved in this browser (existing click-only preferences are retained).
Clicking a linked mention opens an entity dialog
with its canonical name, aliases, known facts, and source chapters. **Edit glossary
terms** expands the existing CRUD controls inside the dialog, initially filtered
to related entries. **Show all visible glossary terms** broadens that list without
changing the reader's spoiler boundary. New-term fields are suggestions only;
check the original source spelling before saving. Changes do not rename entities,
edit facts, or rewrite already-translated text.

Pipeline-recorded named mentions with no entity ID open an empty card: **No linked
information yet**. They have a dotted underline, make no entity API request, and cannot
edit an invented identity. Names are extracted from the saved chapter text by an offline
model pass, not by capitalization rules; their offsets are checked against exact text.
Existing verified links take precedence. Detection can still miss or misclassify a phrase,
but it cannot invent facts or merge identities. Chapters not yet indexed show an explanation.
The dialog and glossary
use the chapter response's server-authorized `at`; caches are separated by novel,
chapter, and clearance. Optional glossary `entity_id` values are also gated by the
linked entity's first-seen chapter. Human seeds without an ID can appear in the
related list by a known source name, but this does not bind graph identities.

Run `npm test` for mention segmentation tests and `npm run build` for the production check.

## Reading and enrichment status

Opening a novel (including its direct URL) lands on **All chapters**, with the range
containing the saved reading position selected. Tabs show 100 chapters at a time:
**1–100**, **101–200**, and so on; only the active range is fetched and rendered.
Arrow keys, Home, and End navigate the range tabs. Browsing the list does not advance
reading progress or request translation. Empty novels offer **Add chapter**.

Selecting a chapter replaces the list with the reader or its pending preview.
Next/Prev keep the list out of the reading view. Choose **All chapters** to browse
again, then **Back to reading** to return to the selected chapter. There is no
separate show/hide chapter-table toggle.

The chapter table distinguishes Not queued, Queued, Processing, Ready and Failed.
Ready means validated prose can be read; Facts pending/unavailable reports graph work
separately. The processing timer is total chapter time, not current-stage duration.
A failed extraction never revokes a saved translation. Unknown facts remain absent.
