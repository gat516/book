# Deployed Ask AI reasoning pilot — preregistered 2026-09-22

User approved the smaller **30-completion pilot**, not the 90-call study.
Hard ceilings: 20 answers, 10 automated judgments, 10 embedding lookups. No
chapter processing, production setting changes, retries, or replacement samples.
Account and book are the same as the deployed gateway smoke; clearance is 170.

## Question and method

Does requesting `reasoning_effort=low` improve the generation phase of deployed
Ask AI compared with its current omitted-effort setting, without an obvious
quality regression? Both use the saved DeepSeek provider, admission gateway,
existing Ask AI system prompt, and an explicit 8192-token output ceiling (the
current adapter default). No temperature, model, context, or output-limit change.

`reasoning_pilot.py` contains ten fixed, generic reader questions: summary,
objective, obstacles, relationship, conflict, capability, setting, chronology,
causality, and an intentionally unsupported bibliographic detail. They are not
selected from successful responses or production latency. Each question has one
pair. Seed 20260922 shuffles question order and balances current-first/low-first
five each. Independent seed 20260923 balances judge A/B placement five each.

Each pair embeds once using the saved embedding settings, retrieves once using
the deployed Ask AI retrieval function, and reuses byte-identical context for
both answers and their judge. Ownership, SQL/RLS chapter predicates, and
read-only transactions enforce spec §0.3, §8.2, and §15. A private evaluation
process uses the deployed `LLMProvider` seam (§5.4); no service restart or image
change is needed. Both arms reserve/settle through admission (§14.7). Gateway
priority and contention are **not** the independent variable in this study.

Calls are sequential with at least seven seconds between starts within a pair
and seven seconds between pairs. A fresh evaluation process/client is created
per pair; first-call cold-client effects are balanced, not eliminated. The node
checks the idle queue, saved progress, unchanged chapter metadata, deployment
images/settings, and gateway capacity. Abort on an infrastructure/provider
failure, changed book state, or invalid judge response; keep all attempted
samples, including the failure, and do not spend the rest of the budget.

The low-effort judge sees context, question, and randomly labeled answers, but
not effort, latency, token counts, or arm names. Its fixed rubric scores each
answer 0–4 for groundedness, completeness, citation support, and instruction
following, plus counts unsupported claims. Automated citation checks verify
labels against the exact supplied sources, but cannot verify entailment.
The same-model judge is exploratory evidence, **not independent human truth**.

## Analysis fixed before paid calls

- Primary exploratory metric: geometric mean of paired low/current generation
  latency ratios, with a seeded 10,000-resample paired bootstrap interval.
- Also show medians, every pair's latency/token ratio, total/median output tokens,
  input and cache-hit tokens, finish reasons, provider/gateway errors, citation
  validity, and blinded quality scores. Reasoning tokens stay unknown unless
  explicitly reported by the provider; do not infer them from answer length.
- Count errors/truncation in denominators and report incomplete pairs. Compute
  latency ratios only for complete successful pairs, labeled as such. A failed
  or incomplete pilot cannot pass the promising-candidate screen.
- A promising candidate requires all 10 pairs and judges, at least 20% lower
  geometric-mean latency, bootstrap upper bound below 1, fewer total output
  tokens, no extra invalid citations/unsupported claims, and mean summed quality
  (out of 16) no more than one point below current. This is a screening rule,
  **not a statistical quality non-inferiority claim or permission to deploy**.
- No p95/p99 or throughput claims from ten samples per arm. This measures the
  completion phase, including gateway RPCs, not browser/HTTP end-to-end latency.
  Retrieval is timed separately. No simultaneous chapter workload is introduced.
- Token reductions are not automatically equal to invoice savings: caching,
  billing periods, judge overhead, and provider pricing matter. Report measured
  counts before any optional explicitly labeled price estimate.

Only metadata is persisted. Private contexts and answer text exist in pod memory
and requests to the already-approved saved providers; they are not printed or
written to files. Raw reasoning, credentials, raw errors, and SDK logs are never
exported. Evaluation usage is in the study ledger, not the ordinary reader
`account_usage` table. A node-side exclusive run directory prevents an accidental
repeat of the same run. Any new run requires a new paid-call approval.

Provider references checked before testing: [thinking mode](https://api-docs.deepseek.com/guides/thinking_mode/)
(current omitted effort defaults to high) and [model/pricing documentation](https://api-docs.deepseek.com/quick_start/pricing/)
(legacy `deepseek-v4-flash` now served by the Flash family). Record the raw served
family separately from the adapter's normalized model name; do not compare this
pilot with earlier runs as if model revisions were fixed.

## Protocol amendment — before continuation, after the first three pairs

The original idle-queue guard stopped the controller before pair 3 (zero-based)
when ordinary chapter work started. Exactly nine completions and three embeddings
were made; all three pairs and judges completed without provider errors. Saved
reader progress changed from 175 to 176 and chapter 181 finished. The pilot did
not enqueue that work. Its first segment, particularly pair 2, may overlap that
background work; metadata does not establish exact provider overlap.

The owner explicitly approved finishing **only the remaining 21 completions**
while leaving QiReadr idle. Continue unchanged questions, order, settings, gate,
and rubric at pair 3; no replacement/repeated samples. Use a new exclusive run
directory and verify the first ledger contains precisely pairs 0–2 and nine
attempts. A second interruption stops again. Total ceiling remains 30 completions
and 10 embeddings across both segments. Production settings and workers remain
untouched. Recheck the current book state before resuming.

Report combined results as a **resumed exploratory pilot**, not an uninterrupted
idle benchmark. Show the original segment and continuation separately as a
descriptive sensitivity check; do not discard original samples to improve the
headline. The original promising-candidate screen remains only a screen, with
this added workload-confounding limitation and no automatic promotion.
