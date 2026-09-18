# Records experiment

Develop and review the extraction behavior here before integrating it into the worker.
The worker stays paused during this experiment. Results are local artifacts, never
published knowledge, glossary changes, queue jobs, or translation changes.

`records_trial.py` reads a saved chapter case and the book's existing provider settings
using a read-only database connection. Each invocation makes at most one completion.
The exact request, source hash, raw response, structural validation, served model,
token usage, and elapsed time are saved before semantic review. An identical completed
request is reused locally. Provider errors are saved without exception prose.

```bash
cd services/pipeline
../../scripts/with-env.sh .venv/bin/python experiments/records_trial.py \
  --novel 1d260de8-e3c8-458e-b92c-d09e55d3fede \
  --case /tmp/book-compact-trial.json \
  --prompt experiments/prompts/compact-v2.txt --reasoning low
```

Change one variable at a time. Inspect important omissions, unsupported additions,
subjects/direction/negation, citation support, and cost. Structural acceptance is not
a factual accuracy score. Chapter 1 is the development case; later validation must use
unseen chapters. Do not insert chapter-specific answers into prompts. The owner reviews
the proposed reader-facing output before any production integration.

## Downstream experiments

`downstream_trial.py` adapts saved grouped notes to the existing atomic normalization
contract, then calls the real identity and rendering stage methods through a local
completion recorder. The normalization trial exhausted an 8,192-token output ceiling.
Truncated calls have unknown usage because the provider abstraction raises before
returning usage; do not report them as zero-cost calls.

`compact_downstream_trial.py` tests an alternative representation: retain the notes,
resolve source-grounded entity groups and note links in small batches, then translate
the notes in small batches. Code retains chapter and citation metadata. This is explicitly
not the existing atomic fact/relation/event storage contract. No database publication
or website API is exercised, and the initial trial supports chapter 1 only (no prior
entity registry). No English name or spelling comparison becomes identity authority.

```bash
../../scripts/with-env.sh .venv/bin/python experiments/compact_downstream_trial.py \
  --extraction experiments/results/memory-v4-medium-80129cf4949848cc.json \
  --output experiments/results/batched-downstream-chapter-1-small \
  --link-prompt experiments/prompts/link-memory-batched-v1.txt \
  --notes-per-call 2 --link-output-tokens 900 --link-reasoning none \
  --render-notes-per-call 3 --render-output-tokens 900 --plain-output \
  --admission-retries 3
```

Completed requests are reused by exact request hash on resume. Failed attempts are
archived. The optional bounded admission retries respect provider cooldowns; the
default is zero retries. Results include English
notes, entity links, source evidence IDs, and successful-stage token counts, plus
explicit limitations. Passing structural checks is not a factual accuracy score.

This configuration completed five identity and three rendering calls for the nine
saved notes. Including the saved extraction, successful requests used 13,583 input
and 5,894 output tokens. See `DOWNSTREAM-REVIEW.md` for quality issues and
`results/batched-downstream-chapter-1-small/reader-preview.md` for the actual output.
