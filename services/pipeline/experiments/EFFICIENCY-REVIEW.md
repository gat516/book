# Incremental efficiency benchmark — 2026-09-17

The owner explicitly authorized sending saved chapter notes/evidence to the book's
configured Groq provider, bounded to ten requests. The trial used Qwen with reasoning
disabled and a 900-token output ceiling. Saved extraction was reused.

Ten requests were attempted: six completed, three were rate-deferred, and one exhausted
the output ceiling. Completed identity calls used 5,567 input and 1,822 output tokens
(7,389 total). Including saved extraction, reported successful usage is 14,027 tokens.
Failed/deferred usage is unknown. The chain is incomplete: the sixth identity response
contains a duplicate linked ID and fails validation before accepting note 8; note 9,
the unresolved follow-up, and rendering have not run. This is not an end-to-end savings
measurement or an acceptable replacement for the 19,477-token baseline.

The initial estimator packed three dense notes and truncated. Increasing its output
work multiplier from 1.4 to 2.7 let the first batch complete at 1,693 tokens. A subsequent
response used an equivalent four-field ordered-name entity shape; local decoding now
accepts it without regeneration, retaining source grounding and explicit ID checks.

Quality remains inadequate: notes 2 and 3 contain passage IDs instead of useful unresolved
names; note 5 leaves its companions unresolved. Note 8 contains generic descriptions as
aliases. Shape validation cannot establish alias semantics. Candidate retrieval and
context reduction need further source review before any production integration.

Artifacts are ignored under `results/efficient-downstream-chapter-1/`. Automatic review
rejected an attempted resumption with admission retries, interpreting it as potentially
exceeding the authorized request count. That command never ran; resumptions used zero
automatic retries and reduced limits to retain the ten-request total. Account billing
tier was not verified; rate limits alone do not establish a free-tier account.

## Local fixes after the live review

Repeated occurrences of the identical linked ID are now deduplicated locally, with a
diagnostic. Different IDs remain distinct even when their spellings match. Invalid
unresolved references (including passage IDs and unwitnessed phrases) are retained as
uncertainty and explicitly flagged for note review rather than silently discarded.
Follow-up payloads now include accepted links and request a review of all named
participants. The prompt explicitly permits witnessed new entities absent from candidates
and excludes descriptive epithets from reusable aliases. Those semantic prompt changes
still require a new live quality evaluation; structural checks cannot prove correctness.

Summaries now expose identity/render stage states and explain when identity prevented
rendering. Rendering already accepts notes with unresolved identities; it requires the
identity stage to return structurally valid results, not every identity to be resolved.
No further provider requests were made for these local fixes. Changing the identity
prompt changes request hashes, so the corrected prompt cannot reuse the old responses
as if they answered the new request.

## Authorized second live run

The owner authorized ten additional Groq attempts using the same chapter evidence.
Results are under `results/efficient-downstream-chapter-1-v2/`. Nine calls completed
(seven identity batches, one unresolved follow-up, and one rendering batch); one
attempt was rate-deferred. Its safe quota fields showed a 1,000 output-token-per-minute
allowance. Resumption paced calls sixty seconds apart and had no further deferrals.
The limit remained ten attempts across both invocations.

Successful logical-chain usage, including the reused extraction, is 13,408 input plus
5,316 output tokens = **18,724 tokens**. New successful downstream usage across both
invocations is 12,086 tokens; the deferred attempt's usage is unknown. Summary
`new_successful_usage` and `unknown_usage_attempts` describe only the latest invocation,
so use the archived deferred artifact and earlier completions for the entire experiment.
Four notes have not been rendered. This is not an end-to-end improvement over baseline.

The second identity response supplied scalar unresolved strings; local decoding now
preserves each as a single reference. New characters omitted in the earlier trial were
returned, but source script conversion still left armor and Lawrence unresolved. The
one follow-up did not resolve its selected reference. Identity stages completed
structurally, which allowed rendering to run.

Rendering returned five notes but failed provisional spelling validation for a
single-character personal name (the existing Pinyin planner supports two to four).
Independent review found more serious errors: note 1 changes the actor/recipient and
mixes the Dark Nightmare marker into character references, while also changing one
marker's brackets and omitting markers elsewhere. Fixing the single-character name
case alone would not make this response acceptable. Do not publish or silently repair
its factual content. Marker-based rendering needs redesign or comparison with plain
text plus a shared naming map before promotion. All original responses are preserved.

Seventeen focused offline tests pass after the scalar normalization and request-pacing
changes. No production data or worker configuration was modified.
