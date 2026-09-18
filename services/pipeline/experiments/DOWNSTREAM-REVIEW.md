# Downstream cost experiment — 2026-09-17

The original downstream contract is expensive independently of discovery quality.
It expands the nine accepted reader-memory notes into atomic assertions, lists those
assertions in an accounting section, creates an entity inventory, and emits separate
records carrying source quotations, citations, and repeated semantic metadata. Identity
resolution and English rendering are additional calls. The existing normalizer exhausted
an 8,192-token output ceiling without returning a usable result.

Output budgets include model reasoning. The current provider abstraction raises on
truncation before returning usage or partial text, so this failed call does **not**
establish how many tokens were reasoning versus visible records. Failed calls may cost
tokens even when the local artifact has no usage values. The proposed 16,384-token
normalization retry was interrupted and did not produce an artifact or running process.

## Alternative being tested

Preserve the accepted notes, resolve their entity links in one call, then translate the
notes in a second call. Code attaches existing citations and chapter metadata. This is
a different storage representation from the existing atomic fact/relation/event tables;
it has not been integrated into the website. Identity remains a model decision;
deterministic checks never merge entities by spelling.

| Local trial | Reasoning | Input | Output | Result |
| --- | --- | ---: | ---: | --- |
| Existing atomic normalization | Low | Unknown | Unknown | Exhausted 8,192-token ceiling |
| Verbose note links v1 | Low | Unknown | Unknown | Groq JSON validation failure |
| Verbose note links v1 | None | 3,372 | 3,094 | Returned, but bad identity grouping and source spellings |
| Tuple links v3, implicit indices | None | 3,350 | 1,128 | Returned, but indices link notes to wrong entities |
| Tuple links v4, explicit IDs | None | 3,390 | 1,369 | Returned, but merged opposing factions |
| Tuple links v4, explicit IDs | Medium | Unknown | Unknown | Exhausted 4,096-token ceiling |
| Tuple links v4, explicit IDs | Low | Unknown | Unknown | Exhausted 4,096-token ceiling |
| Three-note identity batch | Low | Unknown | Unknown | First batch exhausted 4,096-token ceiling |

The successful v4 non-thinking response uses 55.8% fewer output tokens than v1.
That is a format/cost result, **not** a usable-pipeline result: merging the fate and
order factions is a material identity error. The implicit-index format saved more
tokens but is rejected because several links point at unrelated entities.

Compact tuple fields still expand locally into readable named fields. Explicit IDs
remain necessary. Missing source witnesses leave an identity unresolved; they never
authorize a guessed replacement or discard unrelated notes. Unknown citations and
ungrounded aliases are diagnosed and omitted, with original responses retained.

## Operational findings

- Successful requests are saved and reused by exact request hash. Extraction is the
  existing `memory-v4-medium-80129cf4949848cc.json` response throughout.
- Repeated 30-second admission waits were fallback values from our wrapper, not exact
  Groq cooldown hints. Later artifacts record `exact_hint`, safe rate headers and
  allowlisted limit labels. A full normal TPM bucket does not prove all other limits
  are available. Do not infer a cooldown is exact merely because it is numeric.
- A later safe error inspection identified `output tokens`, `OTPM`, and `request too
  large`, with a 1,000-token limit. Some 86.4-second hints came from the *request*
  reset header while the normal token bucket was full. Such waits do not fix a
  request larger than the separate output allowance. The next bounded experiment
  uses two-note identity batches and three-note rendering batches, each capped at
  900 output tokens, with non-thinking mode and checkpoint reuse.
- Provider-side JSON validation discarded some responses. Plain output plus local
  parsing saved completed responses for inspection without another paid regeneration.
- One automatic approval-review timeout delayed an attempt before it reached the
  provider; the explicitly permitted single retry proceeded.
- No worker restart, queue mutation, published records, glossary writes, database
  schema changes, or website implementation occurred.

The batched identity prompt separates a primary name from its aliases explicitly.
Each batch receives only its notes, cited passages and one neighboring passage on
each side for speaker context. Prior batches' entity IDs and names are candidates;
the model must explicitly reuse an ID to establish identity continuity. Code never
infers a merge from identical spellings. This tests reuse within chapter 1; it does
not establish cross-chapter identity accuracy.

Raw artifacts are under ignored `results/downstream-chapter-1/` and
`results/compact-downstream-chapter-1*/`. Successful-stage token totals exclude
unreported usage from failed attempts and are not the total experiment bill.

## Completed small-batch run

The bounded experiment completed all local stages: the saved extraction's nine notes,
five identity calls (two notes per call), and three English-rendering calls (three
notes per call). Each downstream request used non-thinking mode and a 900-token
output ceiling. This is nine logical successful calls including the reused extraction.

| Stage | Calls | Input tokens | Output tokens |
| --- | ---: | ---: | ---: |
| Saved extraction | 1 | 4,362 | 2,276 |
| Identity batches | 5 | 8,143 | 2,792 |
| English batches | 3 | 1,078 | 826 |
| Total | 9 | 13,583 | 5,894 |

The downstream work used 3,618 output tokens in total. The old atomic normalizer
alone exhausted its 8,192-token ceiling; its actual usage is unavailable, so this
is not an exact savings percentage or an equivalent-storage benchmark. More calls
repeat some input but bound each task and allow successful checkpoints to be reused.
The successful chain totals 19,477 tokens; failed experimental usage is excluded.

Artifacts: `results/batched-downstream-chapter-1-small/summary.json` and
`results/batched-downstream-chapter-1-small/reader-preview.md`. The preview preserves
actual model wording without manual corrections.

### Quality review and limits

- Ling Feng / Long Fei share one model-resolved identity, reused across batches.
  Several other recurring characters also reuse IDs. The opposing-faction merge
  observed in the whole-chapter trial did not recur.
- English batches share only the two saved glossary terms. Other terms drift:
  星神造化訣 becomes both “Star Divine Creation Formula” and “Star God Creation Art”;
  星脈 becomes “Star Veins” and “star meridians”. Some personal names are poorly
  rendered, including “Tayi”, “Shcheiko”, and “Long Ze Liyue”. A shared naming map
  is the next experimental correction before rendering.
- The 42 entity entries are not 42 claims: there are still nine grouped notes.
  The registry contains duplicate identities (the Fate God and Raziel are separate;
  底城河 occurs twice), generic terms, and rough types. “殿內” was accepted as an
  alias for the Order Temple, but should not become a reusable global alias.
- Structural checks do not establish semantic correctness. Some unresolved fields
  contain phrases rather than names, and some linked entities occur only in adjacent
  context. The existing extraction's minor overstatements remain, and rendering
  introduces details such as plural bracers.
- This demonstrates completion of local model stages, not readiness for publication.
  Cross-chapter identity, database storage, and reader API compatibility remain
  untested. Grouped notes require a deliberate integration decision because current
  production storage expects atomic facts, relations, and events.

No production worker, database, glossary, or website changes were made by this run.
