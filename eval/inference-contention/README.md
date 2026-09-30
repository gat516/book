# Deployed inference contention experiment

For the separately authorized 30-call current-vs-low reasoning pilot, see
[`REASONING_PILOT.md`](REASONING_PILOT.md) and `reasoning_pilot.py`. That pilot
isolates the completion phase with the gateway enabled in both arms; it does
not test gateway-induced speedup or enqueue chapters. `--analyze` is offline
and never makes provider calls. New paid runs require fresh authorization.
The [2026-09-22 pilot report](results/2026-09-22-reasoning-pilot.md) records
19 attempts before an output-limit safety stop; it does not claim an optimization.

`measure.py` runs on the existing k3s node using Python 3 and `k3s kubectl`.
It measures the running Ask AI HTTP service before, during, and after one chapter
is processed by the ordinary deployed worker. It does not install the gateway or
change application images, provider settings, account controls, or reader progress.

This is real, paid inference: obtain the owner's authorization to send their chapter
context to the saved providers and to process the specified unfinished chapter.
The script requires explicit novel/account IDs, saved reader clearance, and a chapter
number. `--enqueue` authorizes just that chapter; without it, only baseline runs.
Defaults are six baseline requests, at most twelve during processing, and six after.
Requests are sequential with at least seven seconds between starts.

Run `python3 measure.py --help` for arguments. Capture stdout as JSON Lines in the
operator's private measurement artifacts. In the repository, store only reviewed
metadata and aggregate results, never credentials, chapter bodies, or model answers.

## Interpretation

- `seconds` measures `/ask` through the deployed service, including retrieval and
  provider completion. It excludes Cloudflare, the reader API and browser latency.
- The caller uses internal bearer authentication and account identity. Ask AI still
  checks ownership and chapter gates; the script checks against saved progress first.
  The reader API's own admission limiter is outside the measured path.
- `active_ticks / observed_ticks` measures observed overlap with chapter processing.
  Redis stages are sampled every half second. A model-using stage is not exact proof
  of simultaneous provider requests: cache hits and local work can share the stage.
- `served_by` and `sources` distinguish real inference from fast no-context responses.
- Usage and failure snapshots contain only scoped metadata. Usage tracks successful
  completions, not all provider attempts. Check safe deferral counts in pipeline logs
  as well as the durable failure ledger before claiming no provider-limit errors.
- Compare successful, overlapping requests with both baseline and recovery. Report
  sample sizes, medians and ranges. This bounded screening run cannot establish p99
  or the system's capacity, and a latency change alone does not prove quota contention.
- The source fingerprint checks that the retrieved chunk identities stayed the same.
  The final usage-event ledger allows each request to be matched to its reported input
  and output token counts. Reasoning, output length and provider caching may still vary.
- Consider gateway admission only if repeated observations show shared-quota errors
  or reproducible contention. Daily quota exhaustion, missing keys and provider
  outages need different remedies.

The queue mutation refuses paused/focused-away accounts, translated/discarded/failed
chapters, scheduled retries, and novels already queued. The worker may enqueue the
same chapter's normal enrichment after publishing its translation. Completed results
are retained; there is no rollback of generated prose or feature data.

Local guard checks:

```sh
services/pipeline/.venv/bin/python -m pytest -q eval/inference-contention
```

Analyze a completed or interrupted run without making more provider calls:

```sh
python3 eval/inference-contention/analyze.py /path/to/measurement.jsonl
```

The analyzer excludes failed/no-model responses from successful-request latency,
keeps errors in the attempt denominator, rejects missing observation coverage, and
separates full (at least 95% of observed ticks) from partial chapter overlap. A model
or context change suppresses the comparison ratios. A missing finish event marks the
report incomplete. No percentile beyond the median is presented for this small run.

## A defensible optimization claim

Use this run to select a bottleneck, then predeclare a larger comparison with the same
model, provider, chapter clearance, retrieval settings, representative question set,
and offered arrival rate. Repeat matched baseline/optimized runs in alternating order
to reduce time-of-day and cache-warmup effects. Preserve correctness and citation gates.

Report request count, success rate, provider-limit rate, median and (with adequate
samples) p95 latency with uncertainty. Keep pipeline throughput and token usage as
guardrails: an apparent Ask AI improvement obtained by starving processing or using
less context is a different tradeoff. One background chapter and sequential requests
do not measure saturation, maximum throughput, or production p99.
