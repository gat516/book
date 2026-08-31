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

## Exact source passage references

`evidence-v4-bounded-candidates` offers bounded, unchanged source passages with stable IDs.
The model supplies `passage_id` for names, identity decisions, claims and alignments;
it cannot supply quote text or offsets. The application resolves each reference against
the current saved source and retains exact code-point offsets, including for repeated
passages. Invalid references do not publish claims or identity links. An independently
verified decision is still required: a real passage alone does not establish a fact.

Name discovery returns a flat `names` array plus `reviewed`, with every ontology kind
required and set to `true` for each bounded request. The array is capped at 64 rows per
request; application-owned aggregation across passage batches has no contradictory
chapter-global cap. Each name carries its ontology kind and cited passage ID. Empty
lists are allowed; invented names are not. Coverage is reported per
kind and conflicting kind proposals are withheld. These checks prevent silently
omitting a kind from the response; they cannot prove that a model found every name.

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
translations. A small conventional-transcription lookup supplements the model for
recognized English names (including complete middle-dot-separated names), so model
misclassification does not reduce those suggestions to pinyin. Restorations/titles always require human approval, even
when there is one choice. They never establish identity or approve facts. Organizations,
places, and other non-person terms stay in the semantic glossary/translation path.

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
