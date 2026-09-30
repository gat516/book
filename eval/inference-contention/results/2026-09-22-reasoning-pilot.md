# Deployed reasoning pilot — 2026-09-22

**No demonstrated optimization.** The lower-effort candidate did not improve the
completed comparisons. The run also exposed a concrete failure: an ordinary
current-settings answer exhausted its 8,192-token limit after 38.27 seconds,
with 7,594 reported reasoning tokens (92.7% of output). The adapter correctly
rejected the truncated answer; the study did not retry it or conceal its cost.

The owner authorized up to 30 completions and 10 embeddings. Actual use was
**19 DeepSeek attempts**: 12 successful comparison answers, six successful
blinded quality judgments, and one truncated comparison answer. Seven embedding
lookups completed. **Eleven completion calls and three embedding lookups were
not used.** No production defaults changed and no chapters were enqueued by the
study. Gateway admission remained enabled in both arms.

## Completed matched comparisons

These numbers compare only the same six questions with successful answers in
both arms. Each pair reused the exact retrieved context/system prompt and had
equal input tokens and an 8,192-token output ceiling. Retrieval was restricted
to chapter 170, regardless of later reader progress.

| Metric | Current settings | Explicit low effort |
|---|---:|---:|
| Successful matched answers | 6 | 6 |
| Median generation time | 10.29 s | 11.03 s |
| Total output tokens | 10,424 | 11,941 |
| Reported reasoning tokens | 8,890 | 10,431 |
| Total input tokens | 9,649 | 9,649 |
| Automated quality mean, out of 16 | 16.00 | 15.67 |
| Automated unsupported-claim count | 0 | 1 |
| Invalid/malformed/future citation labels | 0 | 0 |

Low effort used **14.6% more output tokens** on the matched questions. Its
geometric-mean paired latency ratio was **1.090** (9.0% slower observed);
the exploratory paired bootstrap interval was 0.970–1.221. This very small,
interrupted, incomplete sample does not establish a population-level slowdown
or speedup. Low was faster on two of six questions. The same-model blinded judge
flagged an unsupported claim/citation-support problem on low's chronology
answer; that is a screening signal, not independent human adjudication.

| Question | Current seconds | Low seconds | Current output tokens | Low output tokens |
|---|---:|---:|---:|---:|
| Obstacles | 17.637 | 19.692 | 3,426 | 3,678 |
| Causality | 11.986 | 15.415 | 2,216 | 3,066 |
| Relationship | 10.655 | 9.336 | 1,797 | 1,807 |
| Unsupported bibliographic detail | 1.270 | 1.394 | 165 | 140 |
| Setting | 3.435 | 3.249 | 709 | 567 |
| Chronology | 9.919 | 12.732 | 2,111 | 2,683 |
| Conflict — incomplete pair | 38.274, truncated | Not called | 8,192 | Not called |

The failed current-settings conflict question has no low-effort counterpart.
Do **not** compare current's all-attempt output total (18,616, including that
failure) against low's 11,941 and call the difference a saving. All seven
current attempts and six low attempts remain in the failure/usage denominators.
The original candidate screen failed and no candidate was promoted.

## Interruptions and scope

The [preregistered protocol](../REASONING_PILOT.md) called for ten fixed questions,
balanced current-first/low-first ordering, a separately seeded blinded judge,
and automatic stopping on provider failure or non-idle book state. It measures
the deployed **completion phase**, including gateway RPCs, not browser/HTTP
end-to-end latency. Retrieval was timed separately (median 0.460 seconds).

The initial segment started at 23:46:22 UTC and stopped after three pairs/nine
calls when ordinary chapter processing restarted. Reader progress advanced
175→176 and chapter 181 completed. Exact provider overlap with the original
segment is unknown. The owner then explicitly approved finishing only the
remaining 21 calls while leaving the app idle; completed questions were not
repeated. The continuation ran 23:55:38–23:58:19 UTC, completed another three
pairs, and stopped on the current-settings conflict answer's output limit.
Both segments had the same schedule hash and question/settings definitions.

Segment sensitivity (descriptive, not alternative headline selection): the
first three pairs gave a low/current geometric-mean latency ratio of 1.080;
the next three completed pairs gave approximately 1.101. Neither segment
shows a clear speed win. Because the run ended early, completed pairs are not
fully order-balanced (four low-first, two current-first). No p95/p99,
throughput, or maximum-capacity claim is supported.

Final verification found an idle queue, zero outstanding gateway permits and
both permits available, with public health `ok`. Later ordinary use advanced
progress to 177 and completed chapter 182; the first corresponding pipeline
usage record was 23:58:39 UTC, after the continuation controller ended. The
study itself used read-only database transactions and never queued chapter
work. Evaluation usage is in this study ledger, not normal reader usage rows.

## Gateway and total usage

For the 18 successful completions, Reserve + Settle overhead was a median
**3.423 ms**, maximum **3.696 ms**. There were no observed provider-limit or
admission errors. The failed completion also released its permit, confirmed by
the final status check. This supports correct, low-overhead admission—not a
gateway-induced speedup, because both comparison arms used the gateway.

Across all 19 attempts, including the judges and truncation, reported usage was
**36,876 input / 43,554 output tokens**. Judges accounted for 15,085 input /
12,997 output tokens. Counts are usage evidence, not an invoice or a projected
production saving. Requested model was `deepseek-v4-flash`; every response
reported raw served family `deepseek-flash`. The adapter normalizes that alias;
the response does not pin an immutable underlying model revision.

The useful next hypothesis is reducing unnecessary reasoning for grounded Ask
AI answers, with a fresh quality-preserving test. Merely requesting `low` did
not achieve that here. Disabling thinking is a separate treatment and has
**not** been tested or deployed by this pilot. Raising the output limit alone
could mask truncation while increasing latency/cost. No automatic changes are
justified by these results.

Evidence: `2026-09-22-reasoning-pilot.jsonl`, `.summary.json`, and
`.environment.json`. They retain timings, bounded usage/error categories,
source/prompt hashes and numeric scores only—no chapter bodies, answers, raw
reasoning, provider keys, or raw errors. Fifty-five focused harness/provider
tests pass. Working-tree changes remain on `feat/hosted-gateway-trial` and are
not committed.
