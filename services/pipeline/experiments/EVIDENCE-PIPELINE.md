# Lightweight evidence pipeline

This is a complete experimental path for review, not the production records worker.
It reuses existing hosted provider credentials. Consumers install no model, tokenizer,
NER service or other dependency. The CLI below is an offline developer tool; no reader
request invokes ingestion or enrichment (spec §0.7).

## Default work: one completion, with bounded repair when needed

The model walks consecutive reading windows through the entire chapter, selects
developments first, then establishes their chapter-local identities in the same response.
Windows contain only passage ranges: source text is supplied once, and no per-window
calls are added. An uncertain identity must not suppress a supported development.
It emits passage IDs and explicit participant IDs, not Chinese summaries,
English notes, copied quotations, or normalized atomic facts. Code attaches exact source
text, offsets, knowledge chapter, and source hash. The resulting excerpts retain the
source's wording, predictions and conditions, though the model can still select insufficient
context or link the wrong participants. Structural checks are not semantic review.

`evidence-policy.json` provides genre-independent defaults for durable identities,
relationships, abilities, commitments, outcomes and world rules. `--policy path.json`
overrides these as novel data. Entity kinds come from the saved novel ontology.
There is no fixed record-count quota. A smaller knowledge index does not remove the
saved source: `search` can retrieve details omitted from the index without model calls.

Identities cite a minimal witness set (target two passages, not a destructive cutoff).
Code finds other spelling occurrences locally. Those occurrences carry no entity ID:
spelling matching is candidate discovery, never identity resolution (§5). Unwitnessed
aliases are omitted. An unwitnessed primary name remains unresolved without discarding
the associated evidence. Duplicate IDs in a link list and unambiguous ordinal formatting
(`p067`/`p67`/`67`) are normalized locally; nonexistent citations are never guessed.
If the model puts a declared local ID into `unresolved`, code expands it to its
source-grounded names for review. It remains unresolved even when that entity has a
`same_as` decision; this formatting recovery never creates a participant binding.

```bash
cd services/pipeline
../../scripts/with-env.sh .venv/bin/python experiments/evidence_trial.py extract \
  --case experiments/results/memory-v4-medium-80129cf4949848cc.json \
  --output experiments/results/evidence-chapter-1 \
  --max-attempts 1
```

The saved case requires `novel_id`, `case.chapter`, `case.source`, and
`case.ontology.kinds`. The original artifact format already supplies them. Run output:

- `evidence.json`: immutable evidence, source, model decisions and provenance.
- `evidence-preview.md`: readable source excerpts and unresolved-reference indicators.
- `attempts/`: append-only numbered provider-attempt records, including failures.
- `responses/`: successful raw responses keyed by request and served provider/model.
- `last-run.json`: stage result and cumulative usage across process restarts.

`--max-attempts` is the **absolute total provider-attempt ceiling for the directory**,
including prior runs, failed calls, rate-limit deferrals and interrupted calls. It does
not reset on resume. Exact completed requests remain reusable even with zero budget.
Extraction uses prompt-guided pipe-separated R/E lines with provider JSON mode disabled. Each entry is parsed independently; fenced output and list prefixes are normalized locally. Valid entries are saved before repair. At most one repair call receives only broken entries and bounded cited context (1,600 estimated input tokens, 900 output tokens). It cannot overwrite accepted entries. The extraction CLI defaults to a two-total-attempt ceiling; specifying one disables paid repair. If repair fails, usable entries survive and remaining problems stay visible. There are no automatic retries. `--retry-failed` explicitly permits another attempt
within the same total cap, after a saved provider cooldown. New calls are spaced sixty
seconds apart by default. Changing that pacing does not invalidate completed work.
Unknown failed usage is reported as unknown, never as zero.

Replay a completed extraction without a database or provider connection:

```bash
.venv/bin/python experiments/evidence_trial.py extract \
  --case experiments/results/memory-v4-medium-80129cf4949848cc.json \
  --output experiments/results/evidence-chapter-1 --replay-only --max-attempts 0
```

## Optional English notes

English notes are separate offline jobs requested for explicit evidence IDs. They
consume only the selected source passages and a shared naming map. They use readable
names, never opaque in-prose markers. Existing chapter-visible glossary spellings win
over provisional suggestions. The glossary snapshot and initial naming map are frozen
so a job's own outputs do not invalidate its cache on resume.

Rendering also receives short gaps and immediate neighbors from the same saved chapter
to clarify speakers and conditions. These passages are labeled as context, separate
from selected evidence, and deduplicated across records. The policy's `max_context_tokens`
defaults to 400 estimated tokens per request (zero disables it), including added naming
and reference overhead. The existing total input cap still applies. Packing uses original
evidence first so optional context cannot force another call; whole context passages are
omitted when they do not fit. Token estimates are heuristic, not provider billing limits.
Grounded unresolved names may receive provisional spellings without becoming identities.
Changed prompts and check versions create new extraction generations; changed rendering
requests create separate cached drafts. Existing artifacts are never rewritten.

```bash
../../scripts/with-env.sh .venv/bin/python experiments/evidence_trial.py enrich \
  --output experiments/results/evidence-chapter-1 --records r1 r2 --at 1 \
  --max-attempts 3
```

This explicitly raises that directory's total ceiling to three attempts; it does not
authorize three additional attempts. Only actual record IDs from `evidence.json` may
be selected. Jobs pack requested evidence under the policy's input budget. An oversized
record stops before a call rather than truncating its source context. Output ceilings
are configurable; lowering them does not guarantee a valid smaller answer.

Validated drafts are stored separately under `drafts/`; the original evidence is never
rewritten. Failure leaves it usable. Drafts carry an explicit semantic-review-pending
status: generated prose and naming still require human review. Changing rendering inputs
creates a new draft identity without rebuilding extraction. A replay of the same selected
records and options reuses exact saved responses.

## Selective identity follow-up

No unresolved identity triggers an automatic completion. A follow-up is an explicit
offline job for selected records and a stated reader-feature need or identity conflict:

```bash
../../scripts/with-env.sh .venv/bin/python experiments/evidence_trial.py resolve \
  --output experiments/results/evidence-chapter-1 --records r1 --at 1 \
  --reason reader-feature --max-attempts 4
```

It retrieves bounded candidates and asks only about unresolved source-grounded references.
Its response may link a supplied identity or leave the reference unresolved. It cannot
merge existing identities, generate new identities, or rewrite accepted bindings.
Decisions are saved as reviewable sidecars, not automatically promoted into the frozen
artifact or production `state.resolutions`.

For later chapters, `extract --prior earlier/evidence.json` (repeatable) retrieves earlier
candidate identities in the same book and experimental generation. The model must
explicitly select an earlier ID with `same_as`; exact/fuzzy matches never authorize it.
Future/same-chapter, other-book, and other-generation artifacts are rejected before they
can enter a prompt. Prompt/policy/ontology/provider/model/budget changes require a new
generation and chronological re-extraction (§0). Cross-chapter quality remains untested.

## Source search and gates

```bash
.venv/bin/python experiments/evidence_trial.py search \
  --output experiments/results/evidence-chapter-1 --at 1 --query '凌峰'
```

This is simple lexical matching against the full saved chapter, not semantic search or
an identity assertion. It needs neither hosted calls nor local model setup. All optional
jobs and search gate on `source_chapter <= at`, never story time. Glossary retrieval is
also capped at the evidence's chapter. These are local experimental gates; production
adoption still requires the existing API authorization plus database RLS.

## Validation

```bash
.venv/bin/python -m pytest -q tests/test_evidence_memory.py
```

The tests cover source-excerpt integrity, configurable priorities, identity authority,
chapter/book/generation gates, unambiguous format recovery, local occurrence scanning,
cross-process attempt budgets, failed/unknown usage, cooldowns, exclusive job execution,
immutable evidence, offline replay, optional enrichment and failure isolation. A hosted
benchmark and source review are separate from these deterministic tests.

Not implemented here: a local NER runtime, lossy source compression, forced model/provider
switches, a production database migration, or new website controls. The original pipeline
and all published knowledge retain their existing storage contract while this evidence
representation is reviewed.
