# Chapter 1 evidence pipeline benchmark

The chapter-1 experimental knowledge path completed extraction and English rendering
for all eight selected records using the configured hosted Qwen model. This benchmark
reuses the saved chapter source and existing chapter translation; it does not measure
fresh prose translation, production ingestion, publication, or reader rendering.
Optional identity follow-up was not invoked. Nothing was published.

| Stage | Calls | Input tokens | Output tokens | Total |
| --- | ---: | ---: | ---: | ---: |
| Evidence selection and local identity | 1 | 4,363 | 1,573 | 5,936 |
| English notes for all eight records | 1 | 968 | 551 | 1,519 |
| Total | 2 | 5,331 | 2,124 | 7,455 |

Compared with the earlier 19,477-token successful knowledge-stage benchmark, this
uses about 62% fewer tokens. The comparison is not equal-quality: the smaller evidence
index omits developments and still has identity and rendering errors. Neither total
includes fresh chapter translation. This run had no failed or unknown-usage attempts.

## Review findings

- Eight records survived structural validation, with twelve accepted local identities
  and eleven unresolved references. The protagonist's identity witnesses did not
  ground the proposed primary name, so the validator kept that identity unresolved.
- Rendering completed, but r2 turns Ares being stranded into abandoning his position.
  r6 attributes the reply to Long Fei without sufficient speaker context in its cited
  passages. r3 renders a strength rank awkwardly as “Seat strength commanders.”
- Ability and faction records sometimes lack surrounding qualifications; the index
  omits several gifts, promises, and political details. Source-search fallback does
  not make these missing records available as structured knowledge.
- Predictions should remain predictions. The evidence preserves source quotations,
  but the generated English notes need semantic review before publication.

These results establish the token saving and completion of the experimental default
path plus its optional English stage. They do not establish production readiness or
equivalent knowledge coverage. Identity corrections should remain explicit model or
human decisions, never spelling-based bindings (spec §0 and §5).

## Verification and artifacts

An exact offline replay of all eight English records reused the saved response with
zero new provider attempts and zero new tokens. The focused experiment suite passed
43 tests. The immutable extraction is preserved separately from rendering drafts.

Local ignored artifacts are in `results/evidence-chapter-1/`: `evidence.json`,
`evidence-preview.md`, `enrichment-preview.md`, `drafts/`, `attempts/`, `responses/`,
and `last-run.json`. The last-run report describes the zero-cost replay; cumulative
usage still includes both live calls.
