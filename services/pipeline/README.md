# Pipeline runtime and reader cards

## Local graph repair runtime

Apply migrations through `0028_graph_runtime_metrics.sql`. Restart the worker and
Ask AI at an idle chapter boundary after updating the shared `novel-llm` package.
Before another model benchmark, use the worker's environment to run:

```bash
.venv/bin/python -m pipeline.benchmark_knowledge --model qwen2.5:7b-instruct --preflight
```

Preflight performs metadata reads and an admission check only; it never runs Qwen.
Actual benchmark commands reserve Ollama for the whole run. Other updated, direct
Book clients sharing `BOOK_OLLAMA_LOCK_DIR` defer until it is released. External
clients/gateways are not covered. Process exit/cancellation releases reservations.

Graph JSON streams internally and must finish cleanly before caching/publication.
`GRAPH_OLLAMA_TIMEOUT_SECONDS` inherits `OLLAMA_TIMEOUT_SECONDS` when unset; use
900 seconds for this CPU host. `GRAPH_OLLAMA_TOTAL_TIMEOUT_SECONDS` defaults to 1800
seconds per call, excluding admission wait. `GRAPH_OLLAMA_NUM_CTX=16384` and
`GRAPH_OLLAMA_NUM_PREDICT=4096` preserve the evaluated context/output budgets.
Runtime changes produce distinct revision/cache identities; old reports are retained.
No provider fallback, retranslation, or automatic graph activation is permitted.

See [runtime findings and verification](../../eval/knowledge/RUNTIME-INVESTIGATION.md).

## Structured chapter events

Event extraction has its own append-only revision and activation pointer. Preparing or
reviewing events does not quarantine or switch the entity graph, and unresolved names
remain as exact argument surfaces rather than causing the supported action to disappear
(instructions.md §0.2–§0.4).

The extractor reads the already-saved display translation, so evidence and unresolved
argument surfaces match what the reader sees; it never retranslates or receives future
text. The local path is deliberately bounded for this CPU-only host: one structured generation
pass per ordinary chapter, followed by deterministic passage/schema checks. There is no
second model verifier. A revision remains invisible until an operator reviews a sample
covering at least five chapters and 30 expected plot events, then explicitly activates
the exact frozen report. Translation work always has queue priority over event backfill.

```bash
cd services/pipeline
../../scripts/with-env.sh .venv/bin/python -m pipeline.event_rebuild prepare \
  --novel NOVEL_UUID --model qwen2.5:7b-instruct
../../scripts/with-env.sh .venv/bin/python -m pipeline.event_rebuild resume \
  --revision EVENT_REVISION --limit 1
../../scripts/with-env.sh .venv/bin/python -m pipeline.event_rebuild preview \
  --revision EVENT_REVISION --output event-report.json
../../scripts/with-env.sh .venv/bin/python -m pipeline.event_rebuild review \
  --revision EVENT_REVISION --file event-review.json
../../scripts/with-env.sh .venv/bin/python -m pipeline.event_rebuild activate \
  --revision EVENT_REVISION --review-hash REVIEW_HASH
```

`event-review.json` names a reviewer, sets `approved: true`, copies the preview's
`review_hash`, assesses every published event with explicit `correct`,
`arguments_correct`, and `status_correct` booleans, and lists the human-expected events
with `chapter`, literal `arguments` (`role`/`surface` pairs), and optional
`matched_event_id`. Activation requires ≥95% precision,
≥85% expected-event recall and argument quality, ≥95% completion-status accuracy, valid
evidence, and zero critical false completions. `rollback` accepts only an archived event
revision and never changes `active_graph_revision`.

## Exact source passage references

Bounded, unchanged source passages have stable request-local IDs. The model supplies
passage references for names, claims and alignments; it cannot supply quote text or
offsets. Identity selection is smaller still: passages and candidate descriptions are
interned once, occurrences contain only subject/choice references, and the model returns
one choice token per occurrence. The application attaches that occurrence's exact source
context and maps the token to an outcome and durable target before independent semantic
verification. A real passage or selected token alone never establishes identity or a fact.

Name discovery uses 8 fixed nullable slots over adjacent passages packed to 400 source
characters and the hard byte budget.
A full response recursively divides only that source window; it does not replay every
paragraph. Application-owned aggregation has no chapter-global cap. Distinct proposed
spellings then pass a separate eligibility check before occurrence expansion, preventing
generic colors, actions, quantities and descriptive fragments from multiplying into
identity and alignment calls. Each accepted name retains an ontology kind and exact
passage citation; this eligibility check never binds equal spellings to one entity.

Identity requests are packed by their serialized byte size rather than occurrence count,
targeting at most 16 KiB including the separately enforced output schema and refusing a
model-visible prompt above 24 KiB. Runtime diagnostics retain prompt, schema, and input
component sizes without retaining their content. The measured v19→v20 byte breakdown is
recorded in [`eval/knowledge/PROMPT-OPTIMIZATION.md`](../../eval/knowledge/PROMPT-OPTIMIZATION.md).

Claim extraction similarly packs short passages into 1,200-character focus requests while
retaining the constituent passage IDs for exact evidence. Saturated focus requests split
recursively. Runtime diagnostics report request counts, cache hits, fresh calls, split
counts and input sizes; benchmark reports also aggregate calls, tokens, inference time and
stall retries by stage so efficiency changes can be judged beside fact and identity recall.

For a bounded discovery diagnostic on one reviewed saved chapter:

```bash
.venv/bin/python -m pipeline.benchmark_knowledge --base SNAPSHOT_REVISION \
  --dataset ../../eval/knowledge/book-reviewed.json --model qwen2.5:7b-instruct \
  --names-only --chapter 1 --output discovery-report.json
```

This uses a separate staging revision and does not create entities, facts or completed
graph jobs. It measures occurrence detection and correct kind classification, not
identity/link or fact precision. It cannot qualify or activate a graph. Full benchmark
and independent publication review remain required. Old prompt revisions cannot be
resumed with the new extractor; create a new revision and retain historical reports.

## Character-name spelling review

Before translation, exact source names are classified for display: ordinary Chinese
personal names use deterministic pinyin; foreign names transcribed in Chinese receive
suggested restored spellings; distinctive personal titles receive meaning-based
translations; named species, groups, places, organizations, objects, and techniques use
semantic translations. A focused second model pass reconsiders ambiguous terms after
the broad inventory pass, so polyphonic Pinyin is not mistaken for foreign-name
restoration. A small conventional-transcription lookup supplements the model for
recognized English names (including complete middle-dot-separated names), so model
misclassification does not reduce those suggestions to pinyin. Restorations/titles always require human approval, even
when there is one choice. They never establish identity or approve facts. Organizations,
places, and other non-person terms are reviewed into the semantic glossary path.

Existing pending pinyin-only suggestions can be refreshed offline using their original
quoted evidence, without later chapters, re-importing, approval, or queue changes:

```bash
cd services/pipeline
../../scripts/with-env.sh .venv/bin/python -m pipeline.refresh_name_reviews \
  --novel-id NOVEL_UUID --source-term 劳伦斯
# Add --apply to save the suggestions. Omit --source-term to refresh all pending names.
```

Already-approved spellings remain locked. Semantic rendering defaults do not override
an existing glossary spelling; use the glossary correction UI for those.

The review UI exposes the classification. Approval maps Chinese/foreign/personal-title
roles to a hard `character_name` spelling constraint and semantic terms to the softer
`semantic_term` constraint. This lets a stale or mistaken review be repaired without
merging identities or approving graph facts.

## Legacy reader name cards

The following describes the older presentation-only path. Revision-managed graph
repair instead aligns source occurrence IDs, verifies claims, and uses reader
knowledge-status polling; quarantined graph facts remain withheld until review.

Apply migrations through `0022_unlinked_mentions.sql` before running this version.
The reader API must also be updated before publishing spans with a null entity ID.

`display_scan` preserves existing glossary-based identity links, then asks the configured
extraction model for literal names in the display text. Exact text matching supplies the
offsets. Newly detected names have no entity ID: no glossary approval, entity creation,
identity merging, or facts are implied. Capitalization is not the detection rule.
This adds one cached model call per chapter; it can miss or misclassify names.

Spans are published atomically before fact extraction finishes. They retain the existing
chapter RLS gate. The frontend can open an empty card without an entity request, and
hover mode also supports clicking. A completed name index is visible on the next chapter
load; currently the reader does not poll for new mentions while a chapter stays open.

## Existing chapters

With the same environment as the worker, preview a bounded names-only backfill:

```bash
.venv/bin/python -m pipeline.backfill_names --novel NOVEL_UUID --start 1 --end 20
```

Add `--apply` to save it. Only completed chapters are processed. Saved prose, facts,
entities and glossary rows are untouched; existing mention links are preserved.
Rerunning is safe and uses the model-result cache. Text changed during discovery aborts
the update for that chapter. This command does not re-run resolution or promote any
provisional glossary mappings.
