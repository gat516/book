# Provider failures and reader-feature controls

## Investigate one incident

1. Identify novel ID, chapter, and time. A UI “Next automatic retry” time is the
   scheduled retry, not the time the original request failed.
2. Read the named chapter's `enrichment_attempts`, `enrichment_retry_at`,
   `provider_retry_attempts`, `provider_retry_at`, `provider_retry_category`,
   `enrichment_discarded`, and `translation_ready`. Then read its most recent
   `chapter_failure` rows (`stage`, `error_type`, `error_code`, `occurred_at`).
3. Check only that interval in `journalctl --user -u novel-pipeline.service` if the
   safe ledger is insufficient. Upstream exceptions can contain complete source/model
   output and credentials; do not copy raw traces into the UI or documentation.
4. Follow the relevant path below. Do not infer a cause from a generic `stage_failed`
   or from a provider health check succeeding. A credentials check is not inference.

## Request and error paths

- Hosted completions: `packages/novel-llm/src/novel_llm/hosted.py`; Groq wire adaptation:
  `groq.py`. Keep SDK logic here, not in pipeline stages (spec §5.4).
- Groq GPT-OSS structured calls use strict JSON schema, `reasoning_effort=low`, and
  `include_reasoning=false`. GPT-OSS does **not** support `reasoning_format`.
  Explicit reasoning effort from the records stage must be accepted by both Groq and
  HostedProvider (OpenRouter inherits it). Hiding reasoning does not disable computation.
- HTTP 400 `output_parse_failed` / `json_validate_failed` become the safe category
  `provider_invalid_json`. They are neither rate limits nor unsupported schemas.
  The September 17 incident happened during `character_names` using GPT-OSS 120B;
  the failed output alone did not establish output-token exhaustion.
- Normal ingestion skips `CharacterNamesStage`: its separate batched name inventory and
  focused alternative-generation calls were the source of substantial extra traffic.
  `display_names.py` classifies terms in the existing source/display alignment call;
  `term_choices.py` chooses a single provisional spelling locally and retains the first
  choice until hovercard approval/correction. Pinyin conventions remain unchanged.
  Records keep their pinned experimental discovery/selection/normalization core.
- Legacy `character_names.py` / `name_checkpoints.py` remain for explicit offline tools;
  their bounded splitting and validated response reuse are not the normal worker path.
- `provider.py` → `pipeline/batch.py` carries safe categories across sequential batches;
  `pipeline/failures.py` writes bounded codes to `chapter_failure`.
- `pipeline/worker.py` owns durable retry limits. Provider admission waits and generic
  stage retries have separate fields. Respect `enrichment_discarded`; a restart is not
  authorization to resume user-paused chapters.
- `reader-api/records.go` reads safe failure codes during retries as well as exhaustion,
  using existing column permissions and the chapter gate + RLS. It does not read the
  restricted stage column. `web/src/recordStatus.ts` translates categories to prose.
  Old `stage_failed` entries cannot retroactively acquire a specific cause.
- Ask AI has a separate error allowlist in `askai/app.py` and `reader-api/ask.go`;
  update both when adding normalized hosted error categories.

Groq parameter reference: https://console.groq.com/docs/reasoning

The hosted cooldown lives in `services/pipeline/pipeline/inference_runtime.py`.
It reacts to provider hints; it is not a proactive request/token budget scheduler.
Provider wait log lines are deferrals, not successful model calls. Check
`character_name_checkpoint` for historical name work and `fact_first_run` for core
extraction progress; count saved successes without printing response text.
Provider deferrals clear the generic enrichment retry deadline; the due sweep also
guards older rows with both deadlines so a stale retry cannot bypass the cooldown or
provider-attempt limit. Admission logs include only allowlisted numeric token quotas.
Core records completion logs include input/output/cache counts. Identity responses are
checkpointed before rendering and revalidated on exact-request reuse, avoiding another
who's-who call after a rendering deferral.

## UI ownership

`ReaderPane` renders `KnowledgeGraphControls`, which owns the top
`RecordStatusBanner` and one primary action in one panel:

- **Build reader features / Pause building:** queues unfinished readable chapters across
  the book in order, automatically covering earlier chapter dependencies. Preserves
  published work and chapter text. Do not add a separate chapter build/retry button;
  readers should not have to resolve chronological prerequisites themselves.
- **Advanced — Refresh every chapter:** opens a new immutable generation. This differs
  from filling gaps and requires the existing confirmation UI.

`ChapterKnowledgeWorkspace` is diagnostics/review only. Do not add a second build
button, status badge, or status bar there. The chapter list retains book-level controls.

## Focused checks and deployment

From the repository root (no hosted credentials needed for unit tests):

```bash
(cd packages/novel-llm && ../../services/pipeline/.venv/bin/python -m pytest -q tests/test_groq.py tests/test_hosted.py)
(cd services/pipeline && .venv/bin/python -m pytest -q tests/test_term_choices.py tests/test_display_names.py tests/test_character_names.py tests/test_batch.py -m 'not db')
(cd services/askai && .venv/bin/python -m pytest -q tests/test_app.py)
(cd services/reader-api && GOCACHE=/tmp/book-go-cache go test ./... && GOCACHE=/tmp/book-go-cache go vet ./...)
(cd services/web && npm test && npm run build)
```

Reader integration tests use `READER_TEST_DATABASE_URL`. The targeted
`TestRecordsStatusReportsSafeFailureWithinReaderGate` checks existing reader permissions
and hides a newer failure beyond the chapter cap, using temporary fixture rows.

Services are user systemd units: `novel-pipeline`, `novel-reader-api`, `novel-askai`,
`novel-web`, `novel-ingest-api`, `novel-scraper`. Restart only affected units after
checks; Go units build via ExecStartPre. Check ActiveState and NRestarts, reader `/healthz`,
Ask AI `/openapi.json`, web HTTP response, and Redis `jobs:worker:heartbeat`.
Load `.env` with `scripts/with-env.sh`; never print it. No migration is required for
this Groq/error-display fix. Live chapters paused by the user must stay paused.
