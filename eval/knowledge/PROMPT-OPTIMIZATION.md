# Identity prompt optimization — evidence-v20

Measured 2026-09-08 against the archived chapter-1 failure from graph revision
`58ea37a6-5021-4087-a5cd-3d1725a89a11`, batch `identity-37-48`.

## Result

| Metric | evidence-v19 | evidence-v20 | Change |
|---|---:|---:|---:|
| Model-visible prompt | 49,299 B | 6,209 B | -43,090 B (-87.4%) |
| Input data | 39,678 B | 5,582 B | -34,096 B (-85.9%) |
| Schema pasted into prompt | 8,987 B | 0 B | -8,987 B (-100%) |
| Separately enforced output schema | also pasted above | 2,012 B | not model-visible |
| Total serialized prompt + schema | 49,299 B | 8,221 B | -41,078 B (-83.3%) |

The chapter itself is 1,756 characters / 5,058 UTF-8 bytes. The failed batch contained
12 current occurrences, 145 offered choices, 121 choice-context links, and 16 offered
passages. Its size came from serializing the same current/candidate contexts once per
comparison, not from chapter text.

## What changed

- Passages are serialized once.
- Current occurrences and earlier/entity candidates are interned into one request-local
  `subjects` table.
- `choice_subjects` maps comparison choices to interned subjects once; each occurrence
  carries only `[current_subject_ref, allowed_choices]`.
- Existing target tokens are reused for the same application-owned target within a call.
- The selector returns only `{"o1":"choice",...}`. The application attaches exact source
  evidence and maps the token to an outcome/target; the independent verifier remains the
  authority (§0.3, §5.4).
- The JSON schema is still passed to Ollama's structured-output `format`, but is no longer
  duplicated in the identity prompt.
- Identity batches are packed using serialized bytes: 16 KiB soft target including the
  separate schema, with a 24 KiB model-visible hard limit.
- Content-free component sizes are recorded in inference logs, completion runtime metrics,
  and extraction diagnostics.

## Regression measurements

A synthetic dense 12-occurrence matrix with 20 choices per occurrence serializes to
78,657 bytes in the old repeated-row representation. The v20 representation produces a
9,624-byte model-visible prompt and 13,001 bytes including its enforced schema.

Verification on 2026-09-08:

- Focused identity/wire/publication suite: 18 passed.
- Full pipeline suite: 398 passed, 20 environment-dependent tests skipped.
- Python compilation and `git diff --check`: passed.

This is a wire-contract and deterministic-evidence change, so the prompt version was
bumped to `evidence-v20-compact-identity-wire`. A v19 graph revision cannot be resumed;
a fresh v20 rebuild and the normal reviewed-resolution quality gate are required before
activation. No quality result is claimed from the size replay alone.
