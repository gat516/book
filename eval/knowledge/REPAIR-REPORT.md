# Book knowledge repair — review required

**Status: quarantined; no replacement activated.**

Novel: `633308f4-7a11-41e1-af46-5a5ff99c5e16`

The repair snapshot contains **22 saved chapter records**. Source and translation
hashes, glossary content, and reading progress match the snapshot. The original graph
and its audit history are retained. Translations remain readable; empty name cards
remain clickable. Cards and Ask AI withhold the untrusted graph.

## Local model evaluation

The source-reviewed fixture contains **64 named mention cases and 30 fact claims**,
including separate Ling Feng, Jiang Mengyue, group, and temple identities. Claims were
reviewed against saved Chinese source, not the existing machine translation.

| Model | Observed result | Decision |
|---|---|---|
| `llama3.2:3b` | First 34 labeled mentions yielded no source-valid links. Discovery included invalid ontology categories. Even perfect remaining cases could not reach 90% recall. | Failed identity gate; stopped early. |
| `qwen2.5:7b-instruct` | First extraction request exceeded the 300-second timeout. | Incomplete evaluation; not qualified. |

**No local model was selected.** Fact-verification benchmarks were not reached after
the identity-stage failures. Their precision is unmeasured, not zero. Cached resume
wall time is not comparable model inference latency. These results do not establish
that Qwen is inaccurate; they establish that it did not complete this qualification run.

The full replacement rebuild was therefore withheld. The baseline staging revision
`19a5265f-7b98-4df6-a500-13432f05d0c8` has no published replacement facts. Administrative
model-test revisions remain isolated from reader requests. No review or activation
command was issued.

## Implemented controls

Source occurrence IDs, explicit unresolved outcomes, literal evidence checks, separate
claim verification, source-to-translation alignment, and chapter-indexed binding
history connect names to facts without guessing from capitalization. New identities
can exist without facts. Ontology snapshots add places, groups, and descriptions while
retaining existing structured attributes and relationships.

Revision-aware jobs and caches isolate repair from ordinary translation completion
markers. Publication checks revision generation and saved inputs; claim keys make
retries idempotent. Reader policies require the active trusted revision and authorized
knowledge chapter. Status polling invalidates cards and refreshes bindings. Review,
activation, resume, preview, and rollback are explicit local commands; rollback never
silently restores trust to the original contaminated graph.

## Verification

- Pipeline: 174 passed, 1 skipped.
- Reader API: unit and database integration checks passed.
- Ask AI: 4 tests passed, including database quarantine/retrieval checks.
- Ollama adapter: 5 tests passed, including actual served-model identity and context/output settings.
- Web tests and production build passed.
- Browser: repair status displayed; formerly linked Dream Palace card opened as unresolved.
- Live HTTP smoke: chapter readable, old entity URL returns 404, untrusted wiki empty,
  future knowledge-status request rejected, source-ID/evidence fields present.
- Source/translation hashes, glossary, and reading progress: unchanged.

The normal translation worker was resumed. Chapter 24 subsequently completed. Chapter
25 failed the existing glossary constraint because its proposed translation omitted
the locked wording `九神殿 → Dream Palace`. No saved translation or glossary wording
was changed to bypass that check. Qwen's timeout occurred while normal local translation
work was also running, so it should not be treated as a controlled latency comparison.

See [repair-report.json](repair-report.json) for integrity results,
[llama-results.json](llama-results.json) and [qwen-results.json](qwen-results.json)
for tested/untested cases, and [the operator guide](../../docs/knowledge-repair.md)
for commands and the explicit review gate.

## Remaining release gate

A local model must complete and pass qualification before the full chronological
rebuild is attempted. Its resulting graph must then pass source-evidence review and
the numerical acceptance gates before the owner explicitly activates it. Until then,
the safe user-visible result is readable prose with unresolved cards and a repair notice.
