# Structured-output investigation — 2026-09-09

## Finding

JSON syntax is not the sole cause of Ling's errors. The same actor-reversal error
occurred under the identical prompt both with and without schema-constrained decoding.
Changing option order changed the answer. The current model/runtime/prompt combination
has semantic and selection weaknesses in addition to formatting weaknesses.

This does not establish that Ling is worse than Qwen, nor that every Ling deployment
has these issues. We tested the existing `maternion/ling-3.0-tiny:8b` endpoint reporting
Ollama 0.33.3, temperature 0, thinking disabled, 16,384 context and 4,096 output budget.
We did not compare quantizations, thinking modes, templates, or other serving engines.

## What the research explains

1. A grammar restricts permissible next tokens; it cannot decide whether a person
   caused an injury or whether a statement is true. Schema descriptions must also be
   present in the prompt to communicate their meaning. This is explicitly documented
   in [llama.cpp's grammar guide](https://github.com/ggml-org/llama.cpp/blob/master/grammars/README.md).
2. Format constraints can affect task performance, rather than merely changing
   presentation. [Let Me Speak Freely?](https://aclanthology.org/2024.emnlp-industry.91/)
   studies this distinction. [JSONSchemaBench](https://arxiv.org/abs/2501.10868)
   evaluates schema coverage, efficiency and quality separately. Neither establishes
   the cause of our particular Ling failure.
3. A recent preprint, [The Constraint Tax](https://arxiv.org/abs/2605.26128), reports
   increased schema validity alongside decreased answer accuracy for the small models
   it tested. It is supporting context, not a Ling benchmark or universal result.
4. [Research on option-order sensitivity](https://aclanthology.org/2024.findings-naacl.130/)
   shows that reordered choices can change model decisions. Our own reversal experiment
   reproduces this problem locally; choosing a valid ID does not guarantee that the
   model correctly associated it with the intended statement/entity.
5. [Anthropic's grounding guidance](https://platform.claude.com/docs/en/test-and-evaluate/strengthen-guardrails/reduce-hallucinations)
   recommends allowing uncertainty and grounding claims in source quotations. Here,
   source passage IDs let the application materialize the original text. But a valid
   citation alone is not proof that it supports the claim: our recognition error cites
   the exact relevant paragraph while reversing its meaning.

The likely local contributors are inference from these results, not proven internal
mechanisms: overloaded output requirements, weak binding of symbolic IDs to entities,
pressure to fill every entity-role slot, option-position sensitivity, and weak handling
of negation/actor direction. Required nullable slots permit abstention syntactically but
do not ensure that the model uses it. English rendering added an avoidable task on top
of source-language extraction. Fluent explanations are generated outputs, not validators.

## Controlled local experiment

`services/pipeline/experiments/statement_choice_probe.py` supplies full Chinese statement
alternatives, an explicit `none` option and the full original chapter. Each response
only selects evidence IDs and a statement ID; Python copies the immutable statement.
No generated roles, translated values or rationales can contaminate the selected text.
Three questions per call, then reversed option order: four calls for ten decisions.
The same four calls were repeated without grammar, with the same temperature and prompt.
Expected answers and reviewed evidence requirements never enter the model requests.

| Mode | Valid decisions | Correct choices | Correct with reviewed evidence | Call errors |
| --- | --- | --- | --- | --- |
| Schema constrained | 10/10 | 7/10 | 5/10 | 0/4 |
| Prompt only | 6/10 | 5/10 | 5/10 | 2/4 |

Invalid calls count against the denominator. Prompt-only failures exceeded the allowed
number of evidence citations; the error string says `Unoffered evidence`, but the IDs
were real. This is a shape/cardinality failure, not fabricated paragraph identifiers.

Specific observations:

- Rain as the injury cause and Mo Lei as the group leader worked in either order.
- Norton not knowing Ling Feng's identity worked in the original order, then changed
  to an actor-reversed false statement in the reversed order, in both decoding modes.
- The model picked false statements in an all-false question despite an available
  abstention option. Removing free prose did not remove hallucinated decisions.
- The initial/later decision statement was selected correctly but its citations covered
  only the first time point. This fails evidence completeness even though it is true.

Artifacts: `ling-3.0-tiny-ch1-statement-choices.json` and
`ling-3.0-tiny-ch1-statement-choices-prompt.json` beside this report. These small
assistant-reviewed cases are diagnostic, not an owner-approved or held-out benchmark.

## Proposed solution and implemented boundary

Keep Ling for discovery; do not discard its demonstrated useful source-name signal.
Keep source-language extraction and separate optional English display rendering,
consistent with spec §0.2 and §5. The graph's canonical ontology remains per-novel data.

The recommended architecture is:

1. Preserve a candidate's exact wording and application-owned source evidence. Split
   independent propositions before checking them, especially claims with multiple times.
2. Ask narrowly scoped semantic questions. Avoid a role slot for every possible entity.
   For relationships, validate a complete directed assertion (actor, relation, other
   participant, polarity and time), rather than a bare `target` or `cause` token.
3. Let Python assemble JSON and resolve request-local choices. Never let those choices
   bypass RESOLVE or become graph IDs. Structural checks reject unknown IDs, missing
   evidence, extra fields and duplicates; they cannot certify truth.
4. Quarantine failed or unstable decisions. The new local `consistency_report` rejects
   missing/invalid results and changed selections across reversed alternatives without
   consulting test labels. Stable answers remain explicitly unverified: consistent
   errors and incomplete evidence can still pass a consistency check.
5. Require independent semantic review before publication. Until a larger held-out
   test validates an automated verifier, this means human review for graph admission.
   A separate call to the same model is not independent evidence and may repeat the
   same mistake. A qualified alternative model or task-specific fine-tuning could later
   automate this boundary, but neither is validated by these experiments.

Implemented now: the statement-choice diagnostic, source-hash-pinned fixtures, strict
materialization, correctness-plus-evidence metrics, and order-instability quarantine.
All outputs remain `graph_ready=false`. The live artifacts predate the added consistency
summary; raw responses are preserved, and the guard is covered by regression tests.
No production pipeline, graph rows, model installation or worker configuration changed.
Worker resumed after each bounded probe. Fifteen experiment tests pass.

This is a concrete containment design and a better diagnostic, not a claim that we
have repaired Ling's semantic accuracy. Merely changing JSON shape or adding more
self-verification calls is not supported as a complete solution by the evidence.

Reproduce from repository root with the inference worker paused:

```bash
services/pipeline/.venv/bin/python services/pipeline/experiments/statement_choice_probe.py \
  --output /tmp/ling-statement-choices.json
```

Add `--decoding prompt` for the no-grammar control. Production call counts depend on
candidate generation and verification batching. Order checks double selector calls;
they are a diagnostic/quarantine technique here, not a blanket production requirement.
