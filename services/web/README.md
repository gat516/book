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

Open `http://localhost:5173/` for the library, or `?novel=<novel-id>` for a book.
Local development opens directly without login. Vite reads `BOOK_MODE` at startup;
restart it after changing modes. `BOOK_MODE=hosted npm run dev` enables hosted auth
testing. Production builds always resolve `/api/auth/session` through `AuthGate`.

`session.ts` attaches the server CSRF token to mutations. The legacy local reader header
is only a compatibility bridge for an older local API; hosted identity never comes from
localStorage or browser actor headers. See [deployment/auth setup](../../deploy/hosted/README.md).

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

## Hosted model endpoints

Anthropic, DeepSeek, Gemini, and Groq use the providers' built-in official API endpoints.
Account settings therefore ask only for the provider key, while each book chooses its
translation and knowledge model. A stale Base URL saved by an older client is ignored.

For another service, choose **Custom API (OpenAI-compatible)** in the book's model
settings, enter its exact model IDs, and enter the API base URL including its version path
(for example, `https://models.example.com/v1`). Its encrypted key is saved once under
**Account settings → Provider keys**. Custom model health checks call the endpoint's
`/models` route and require its hostname in `PROVIDER_HEALTH_ALLOWED_HOSTS`; this is the
server-side SSRF boundary, not a restriction enforced by the browser.

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

## Reading experience and onboarding

`/welcome` previews the public landing page even in login-free local development.
`/demo` is an original three-chapter story with prepared translations, character facts
and answers; it makes no model or private-library requests. `/privacy` explains stored
data, provider processing and deletion. Production `/` resolves the real session;
local Vite `/` continues directly to the library. Invitation links survive public
sample navigation without storing the invitation in browser storage.

The first-book guide reflects saved provider keys and whether a book exists. The
library offers title search and book covers with chapter progress. Shared responsive
surfaces live in `src/design.css`, retaining Light, Warm, Dark and system themes.
Animate UI Fade/Button primitives are adapted for React 18 with reduced-motion support;
see `src/components/animate-ui/README.md` and `public/licenses/animate-ui.txt`.

`npm run test:components` checks the setup actions, public invitation navigation and
sample chapter boundaries. Run alongside `npm test` and `npm run build`. Component
checks do not replace a connected-browser visual/accessibility review.
### Chapter reader and wiki shortcuts

The chapter view pairs a paper reading surface with a story companion. Its wiki and
Ask AI requests use the lesser of the open chapter and the server's stored clearance
(spec §0.3/§8); moving backward remounts the companion and clears previous answers.
Wiki links resolve a unique saved `source_term`, never an English display-name match.
The API keeps ownership and chapter authorization on every wiki request.

Hovercards use Floating UI for viewport placement, pointer transitions, Escape and
outside-click dismissal. Editing holds the card open. A successful spelling save
closes it and reports confirmation in the reader; a failed save stays open for retry.
Opening a wiki page selects the correct shelf and restores the reading position on
close. `npm run test:components` covers these flows in a DOM test environment, alongside
the onboarding/demo tests. A real-browser visual pass remains a separate check.

### Ask AI answers

Responses render as Markdown with paragraphs, lists, emphasis and tables. Raw HTML
and remote images are disabled. Source labels are matched against the server's
retrieved sources and shown as chapter badges; unmatched labels are marked unavailable.
Formatting does not fetch chapters or change authorization (§0.3/§8).

The answer has its own keyboard-focusable scroll area and an expanded reading dialog
with Escape/outside-click dismissal. The submitted question stays with its answer;
submitting a new question clears the previous answer. Markdown code loads only when
the first answer arrives. DOM tests cover formatting, citation provenance, untrusted
content, dialog focus/dismissal, and request state; they do not exercise a paid provider.
