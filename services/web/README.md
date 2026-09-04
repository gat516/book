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

## Per-book Ollama models over Tailscale

**Book Settings → Model provider** can use one Ollama server for two different jobs:
set the stronger translation model separately from the evidence-gated graph/extraction
model. A recommended local pairing is `qwen2.5:7b-instruct` for translation and
`qwen3:4b-instruct-2507-q8_0` for extraction.

The **Base URL** is the Ollama server URL only — for example
`http://cj-desktop.taila10bf4.ts.net:11434`. Do not add `/api/tags` or another endpoint
path. After saving, **Load models from this Ollama server** calls Ollama's fixed
`GET /api/tags` endpoint and offers the installed model names.

The browser never calls Ollama directly. `ingest-api` does the catalog lookup, and it
accepts only `http(s)` base URLs whose hostname is in `OLLAMA_ALLOWED_HOSTS` on the API
machine. Add the Tailnet hostname there, for example:

```dotenv
OLLAMA_ALLOWED_HOSTS=localhost,127.0.0.1,cj-desktop.taila10bf4.ts.net
```

Ollama defaults to `127.0.0.1:11434`, which a different Tailnet machine cannot reach.
On the Ollama host, bind it to that machine's Tailscale IP (not `0.0.0.0`, which would
also expose it on ordinary LAN interfaces):

```bash
sudo mkdir -p /etc/systemd/system/ollama.service.d
printf '[Service]\nEnvironment="OLLAMA_HOST=100.85.184.34:11434"\n' \
  | sudo tee /etc/systemd/system/ollama.service.d/tailscale.conf >/dev/null
sudo systemctl daemon-reload
sudo systemctl restart ollama
```

Replace the IP with the Ollama host's current Tailscale address. Ollama has no built-in
authentication on this port, so keep the service private to your Tailnet and do not
publish the port through a public reverse proxy.

## Reading and enrichment status

**Processing queue** controls the shared local worker. Opening a book or returning to
its visible browser tab gives its queued chapters priority over other books, including
old explicit chapter-priority requests. This does not enqueue new chapters or change
reading progress. The last focused tab wins; polling never changes priority.

- **Open book first, then background books:** finish eligible focused-book work before
  returning to background books.
- **Only the focused book:** leave other books queued until you switch or change mode.
- **Paused:** finish current work, then claim nothing else. Switching books does not resume.

The panel lists active book titles, short IDs, chapters and stages, plus pending counts
per book. Controls preserve pending jobs and saved translations, and take effect at the
next chapter boundary. Background graph work obeys the same modes; scraping and explicit
CLI benchmark/rebuild commands are separate. Settings persist in Redis (`jobs:control`)
across worker restarts. Both APIs and the worker must be updated together.

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
