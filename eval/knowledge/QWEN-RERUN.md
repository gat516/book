# Qwen rerun on saved chapters — 2026-08-28

## Outcome

The repaired runtime completed Qwen's first extraction call in **412.9 seconds
(6 minutes 53 seconds)**. It was not cancelled at the old five-minute deadline.
The quality gate then stopped the benchmark: chapter 1 produced no source-valid
mentions and no entity links. **No replacement graph was activated.**

There are **23 completed chapters** in the new snapshot, covering chapters 1–24
except chapter 22. Chapters 22 and 25 have error status; chapters 26–50 are ingested
but not completed. The full snapshot remains pending because Qwen did not qualify.
Saved source, translations, glossary text and reading progress match the snapshot.

## Runtime measurements

| Measurement | Result |
| --- | --- |
| Model | `qwen2.5:7b-instruct`, local Ollama |
| Execution | CPU; exclusive Book reservation; no other model resident at start |
| Context / output cap | 16384 / 4096 tokens |
| Idle / total deadline | 900 / 1800 seconds |
| Model loading | 11.0 seconds |
| Prompt processing | 259.2 seconds, 1635 tokens |
| Generation | 142.7 seconds, 477 tokens |
| First output token | 270.2 seconds |

The Intel GPU override still requires administrator access and was not applied.
The inference reservation was released when the benchmark finished.

## What failed

Qwen proposed six real character names, all using the valid `character` kind:
黄少天, 凌峰, 姜梦月, 阿采, 诺顿·威尔森, 莫雷. All six names occur in the saved source.
However, **all six proposed evidence quotations failed exact-source validation**:

- Four match only after removing whitespace and normalizing quotation marks.
- Two move dialogue ahead of its narrative attribution, changing the passage order.

Those observations are diagnostic only. Validation was not loosened and no quotation
was silently repaired or published. The absence of valid evidence caused the pipeline
to reject these proposals before identity resolution, alignment or fact extraction.

Qwen also omitted reviewed place/group names including 九神廷, 啸牙冒险团,
噬梦冒险团, 威尔森家族, 恐惧神殿, 梦魇神殿 and 裁决学院. Its six proposed literal
surfaces cover 13 of the 34 labeled chapter-one occurrences; this is a coverage
diagnostic, not a verified identity score or an alias-matching rule.

The run evaluated 34 of 64 mention cases and linked zero. Even the benchmark's best
possible remaining recall was 46%, below the 90% gate. Link precision is undefined
because there were no links. The 30 reviewed fact claims were **not tested**, so fact
quality is still unknown. Zero published facts does not demonstrate good fact precision.

This establishes a failure of the current extraction setup. It does not establish
that Qwen cannot perform the task with a better input/output contract.

## Next repair to evaluate

Offer exact source passages with stable IDs and have the model reference those IDs,
instead of requiring it to reproduce long quotations. Resolve evidence back to saved
source bytes and retain independent verification. Explicitly check extraction coverage
across the book's entity kinds, including places and groups. Neither change was made
or benchmarked in this rerun; precision and review gates must remain unchanged.

## Artifacts

- Full current-chapter staging revision: `979dc113-3f62-4f99-b064-613ac84f45d4`
- Benchmark staging revision: `69b05c2a-d282-4a86-9a30-338a83c1170f`
- `qwen-runtime-results.json`: measured benchmark result and per-call timings.
- `qwen-runtime-diagnosis.json`: rejected quote and surface-coverage diagnostics.
- `qwen-runtime-review.json`: staged benchmark preview with rejected proposals.
- `current-chapters-preview.json`: all 23 saved chapters and preservation checks.
- Historical `qwen-results.json` is retained unchanged as the earlier timeout result.

The original graph remains quarantined. Empty cards are preferable to unsupported
facts or false identity merges; activation still requires explicit review.
