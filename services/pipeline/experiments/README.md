# Two-pass story-fact probe

This is a read-only local experiment, not a production STATE-EXTRACT replacement.
It never opens the graph database. `claims` means evidence-reference validation passed,
not that the claim is true. `contract_clean_claims` additionally excludes detectable
field errors; it still requires semantic review. `graph_ready` is always false.

## Findings, 2026-09-09

The latest Claude plan (`/home/cj/.claude/plans/plan-peaceful-wreath.md`) retains
source passage provenance. Production `knowledge.py` explicitly stores source-language
fact values, with `value_en` as a separate display gloss. Chinese input is intentional
(spec §0.2, §0.4, §5); translating first changes the evidence and entity surfaces.
English prompt language alone did not require English output in the original probe.

The original compiler supplied a Pydantic schema only through the API's `format` field.
Its prompt omitted the full shape and definitions for importance, temporal labels and
predicate/value decomposition. Both [llama.cpp's grammar documentation](https://github.com/ggml-org/llama.cpp/blob/master/grammars/README.md)
and [Ollama's structured-output guidance](https://docs.ollama.com/capabilities/structured-outputs)
explain why describing the schema in the prompt matters: grammar constraints do not
automatically teach the model their meaning.

The [Ling model card](https://huggingface.co/inclusionAI/Ling-3.0-tiny) describes a
7.9B-total/1.3B-active hybrid MoE, recommends thinking and sampling settings different
from this deterministic compiler, and documents a specific experimental Ollama setup.
The local tunnel reports `maternion/ling-3.0-tiny:8b`, GGUF Q4_K_M, with a Bailing V3
template supporting thinking. This does not prove the community quantization/runtime
matches the publisher's evaluated setup, nor establish a runtime bug.

Same saved pass-1 notes, same Chinese chapter, same model, 4096 output limit:

| Run | Result |
| --- | --- |
| Original free claims array | 15 evidence-valid claims; all four classifications constant; kind=`subject` |
| Revised constrained keyed batches | 15 emitted, 13 evidence-valid, 2 rejected invented link surfaces; all four classifications vary |
| Same revised prompt without grammar | First batch copied schema wrapper keys and emitted invalid JSON; stopped after 470 tokens |

The constrained run used 405–469 output tokens per batch, 2141 total across five
three-candidate batches. Among its 13 evidence-valid claims there were 8 distinct kinds,
2 epistemic labels, 2 temporal labels and 3 importance labels. Eight still had self-links.
Some values remained Chinese; several claims retained unjustified analyst interpretations.
For example, a flash of excitement is still promoted to development/proactivity. Pass 1
has useful discovery signal, but its semantic correctness is not established either.

Artifacts are in `eval/knowledge/ling-3.0-tiny-ch1-{twopass,keyed,keyed-prompt}.json`.
The keyed artifact predates the final added language counters/contract-clean split; its
raw responses and existing quality flags are preserved. The prompt-only artifact is an
incomplete checkpoint containing the failed raw response, not a successful run.

Conclusion: prompt/contract design materially contributes; an inherent constrained-
decoding defect is not established. Several changes were combined, so this is not an
ablation proving which change caused the improvement. The model is still unqualified
for graph writes. No production routing, ontology, worker code or graph rows changed.
Only Ling was installed on the offered tunnel; no Qwen baseline or translated-English
quality comparison was run. Changing to English cannot be assumed to fix these errors.

## Reproduce

From the repository root, with the model endpoint available and competing worker
inference paused for the run:

```bash
services/pipeline/.venv/bin/python services/pipeline/experiments/two_pass_story_facts.py \
  --host http://127.0.0.1:11436 --model maternion/ling-3.0-tiny:8b \
  --replay-analysis eval/knowledge/ling-3.0-tiny-ch1-twopass.json \
  --output /tmp/ling-keyed.json
```

Use `--decoding prompt` for the no-grammar control. Both modes use temperature zero
and validate the output in Python. Replay verifies the input hash (legacy artifacts
derive it from their recorded source path) and skips pass 1. Use `--model` with another
installed model to compare compilers against identical notes; omit replay for a full
two-pass model comparison.

Use `--text-field english` with a dataset containing that saved translation to explore
English extraction. Omit replay when switching text: source notes cannot be silently
reused with translation passage IDs. A missing field raises an error; there is no
fallback to Chinese. This probe's offsets always refer to the selected input text and
must never be used as production source offsets.

`--kinds` supplies the experimental category allowlist. These are claim categories,
not production entity kinds; production ontology remains per-novel data (§0.4).
`--batch-size` accepts 1–4. Null drops a candidate; no padding facts are required.
Uniform classifications are review warnings, not automatically wrong. Language counters
exclude known participant names but can still flag untranslated titles/other proper nouns.

## Single-candidate follow-up

`candidate_verdict_probe.py` runs ten assistant-reviewed chapter-1 cases from
`eval/knowledge/ling-candidate-checks.json`: five supported statements paired with
five misleading statements covering injury/recovery, initial/later decisions,
impersonation/recognition, leadership attribution, and emotion/personality change.
Expected labels and review notes are not sent to the model. The fixture is pinned
to the source hash and supplies exact entity surfaces; this does not measure discovery
or resolution accuracy. Request-local entity choices are never graph entity IDs (§0.3).

Two Ling runs on September 9:

| Mode | Calls | Wall time | Verdict matches | Remaining defects |
| --- | --- | --- | --- | --- |
| One candidate + generated fact/links | 10 | 43.606 s | 10/10 | Every positive has spurious links; one value contains `</arg_value>`; explanations can reverse actors |
| `--verdict-only` | 10 | 12.194 s | 10/10 | Recognition explanation still reverses actors; wrong-leader explanation invents a Norton title and cites inadequate passages |

The second run uses the already-warm model and a smaller schema/prompt; timings are
not a controlled throughput comparison and must not be extrapolated to large chapters.
Its answers were 39–85 tokens each. These are verdict agreement scores, not ten fully
correct grounded outputs. In particular the recognition rejection is the right boolean
for the wrong reason. Neither run qualifies automatic graph writes. The simpler mode
does prevent generated links or rewritten values by removing those fields entirely.

Artifacts: `eval/knowledge/ling-3.0-tiny-ch1-single-candidate.json` and
`eval/knowledge/ling-3.0-tiny-ch1-verdict-only.json`. Worker resumed after each run.

```bash
services/pipeline/.venv/bin/python services/pipeline/experiments/candidate_verdict_probe.py \
  --verdict-only --output /tmp/ling-verdict-only.json
```

For N candidates and K analyst chunks, one-candidate compilation/checking costs
K+N calls; batching B candidates would cost K+ceil(N/B), excluding retries, separate
entity resolution, translation, or any additional verifier stage. For one analyst
chunk, 30/60/100 candidates mean 31/61/101 calls individually, versus 11/21/35 with
three per call. Those candidate counts are illustrative, not a measured big-chapter
distribution. The current ten-case probe skips analysis and makes exactly ten calls.
It sends the full offered chapter each time, so repeated input/prefill cost matters.
No production batching change is justified by this small diagnostic alone.

## Minimal unlinked extraction — September 9

Added `--minimal`: the only model fields are `statement` (Chinese) and `passage_ids`.
The application adds source quotations/offsets and marks every result for review.
There are no entity IDs, links, classifications, English glosses or rationale fields.

| Input / shape | Calls | Time | Output tokens | Outcome |
| --- | --- | --- | --- | --- |
| Saved 15 analyst cards, 3 per call | 5 | 27.397 s | 611 | 15 statements; inherited unsupported assertions and wrong citations |
| Direct chapter, 16 nullable keyed slots | 1 | 4.112 s | 104 | All slots null; no useful extraction |
| Direct chapter, compact statement array | 1 | 6.990 s | 577 | 16 statements; actor errors, lost uncertainty, incomplete coverage |

The compiler consumed fewer output tokens than the prior classified run (611 vs 2141),
but this is not evidence of improved factual accuracy. It copied such analyst errors as
Mo Lei "disguised as human" and an invented conflict between Norton and his own family.
Candidate c15 cited p27, which contains only an exclamation, as support for Norton's goal.

The direct-source array avoided those notes but still misattributed the route instruction
and agreement to A Cai (c8/c9). It asserted skeletal guards appeared (c10), where the
text has a character ask whether they are guards, followed by the mechanical-spider
reveal. It also attributed the scout identification to A Cai without support (c12).
Several items join independent facts. Its citations proceed mostly in paragraph pairs
through p32 and omit the later leadership/identity revelations in this 56-passage chapter.
The cap of 16 is a diagnostic output bound, not a complete large-chapter extraction policy.

Conclusion: removing linking substantially simplifies output and reduces generation,
but does not fix the model's underlying attribution/negation errors. These outputs are
unlinked review candidates only; no production graph behavior changed.

Artifacts: `eval/knowledge/ling-3.0-tiny-ch1-minimal.json`,
`ling-3.0-tiny-ch1-minimal-source.json` (preserved unsuccessful keyed trial), and
`ling-3.0-tiny-ch1-minimal-source-array.json`. The latest source-only code uses the
compact array; the earlier keyed artifact predates that change. Worker resumed.

```bash
services/pipeline/.venv/bin/python services/pipeline/experiments/two_pass_story_facts.py \
  --minimal --source-only --host http://127.0.0.1:11436 \
  --model maternion/ling-3.0-tiny:8b --output /tmp/ling-minimal-source.json
```

For the saved-note comparison, replace `--source-only` with
`--replay-analysis eval/knowledge/ling-3.0-tiny-ch1-twopass.json`.
Eighteen experiment tests pass, including rejecting unexpected linking fields and
materializing only application-owned evidence.

## NuExtract second-pass payload audit

The saved `eval/knowledge/nuextract3-ch1-keyed.json` is the local experiment's compiler,
not production `knowledge.py`'s hosted `_review_hosted_claims` pass. It reuses Ling's
15 analyst cards and sends the full Chinese source plus three cards, generic English
instructions and an inline JSON schema per call. `OllamaProvider` sends system/user
roles and `format`; it does not send NuExtract's native template role.

The installed Q6_K template selects `structured` only when a `template` message exists;
otherwise it selects `content`. This matches the publisher's documented
[Ollama message-role interface](https://ollama.com/numind/nuextract3:Q6_K).
The API JSON schema and NuExtract's extraction template are different inputs.

`nuextract_pass2.py` is an opt-in adapter around the existing OllamaProvider: preserves
priority Class, served-model identity, streaming limits and validation (§5.4), but
repackages compiler requests into native `template`, `instructions`, and `user` roles.
It maps schemas to NuExtract templates, uses verbatim names, explicitly asks for Chinese
values/rationales and English predicate identifiers, and warns that analyst quotations,
categories and passage IDs are untrusted. It distinguishes reported claims, intentions,
reader knowledge and actual events. Actual wire payloads are saved in `native_requests`.
Chinese prose is expected here; the generic CJK counter is observational, not a defect.

Live five-call comparison, same saved analyst notes and model:

- Baseline: 15 evidence-reference-valid claims, zero detected link-field errors.
- Native/Chinese instructions: 15 evidence-reference-valid claims, three bad/self links;
  retained unsupported Norton-family conflict, wrong Mo Lei citation, and candidate-slot
  drift (e.g. candidate c8 about scouts became A Cai possessing a chain).

Artifact: `eval/knowledge/nuextract3-ch1-native.json`. No overall improvement established;
do not promote this adapter as the new default. Language, template task and instructions
changed together, so this does not isolate the effect of Chinese prompting. The run
predates the added explanatory language-note metadata. All results remain unverified.
Worker resumed; production code and graph unchanged.

```bash
services/pipeline/.venv/bin/python services/pipeline/experiments/nuextract_pass2.py \
  --replay-analysis eval/knowledge/nuextract3-ch1-keyed.json \
  --output /tmp/nuextract-native.json
```

Next useful payload refinement: replace verbose analyst cards with explicit candidate
records (one source-language assertion and separately marked proposed evidence IDs),
retain the full source for correction, and validate candidate-slot ownership. Do not
silently parse these malformed CSV-like cards by commas: their contents include quoted
commas and inconsistent fields. A new first-pass contract needs its own measured trial.
