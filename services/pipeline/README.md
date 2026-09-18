# Pipeline runtime and reader records

## Records enrichment

Saved chapter prose is published independently of knowledge enrichment. The production
records worker follows the pinned 96ff9cf contract: atomic discovery, compact selection,
assertion normalization, chronological who's-who identity, native assertion rendering,
then publication. Each prefix is checkpointed in `fact_first_run`, so a retry resumes
from the last validated response. Only who's-who assignments may bind an entity;
uncertain names remain references. Source values, exact qualifiers and evidence are
retained alongside optional target-language renderings, with served provider/model
metadata recorded for every live completion.

Backfill a saved novel without retranslating it:

```bash
cd services/pipeline
../../scripts/with-env.sh .venv/bin/python -m pipeline.records_backfill NOVEL_UUID \
  --manifest .codex/records-backfill.jsonl
```

The command is resumable and queue-deduplicated. It records source object hashes and
durable translation URIs in the manifest; it never writes either object. A new prompt,
ontology, extraction model, or source requires a new record generation. Readers only
see published runs from the active generation at or before their knowledge chapter.

Malformed individual records are dropped with bounded diagnostics; a valid empty chapter
is complete. Rendering failure leaves source records available with an explicit fallback.
The benchmark harness remains available for comparing variants offline; this coding pass
did not perform live-provider parity or rollout; broader validation is deferred per the
user's request.

## Exact source passage references

Source passages are frozen once per fact-first run in `fact_first_passage`. They use
chapter-local `p001`-style ids plus immutable character offsets and the run's source hash.
Discovery and normalization cite those ids; validators check that evidence belongs to the
claim package and that source spans and asserted qualifiers satisfy the pinned baseline
contract.

A citation never establishes identity. The chronological who's-who pass is the only
authority allowed to bind a normalized local proposal to a persistent entity. An uncertain
surface remains an unresolved `fact_first_reference`, and its records retain the source
surface and evidence without an entity binding.

## Call scheduling

Records enrichment normally makes three core calls (discovery, selection, normalization),
plus chronological identity resolution and target-language rendering when needed. Each
validated core response is checkpointed. Empty selections skip unnecessary work. The
provider boundary carries priority, model selection, and served-model provenance (§5.4).
Who's-who is also checkpointed before rendering starts. A retry revalidates its original
response only when the complete request (including earlier entity candidates) matches,
retaining the actual served model. Rendering deferrals therefore do not repeat a
completed identity call. Worker logs report core-call input/output/cache token counts
without source text or model responses.

## One provisional spelling per term

Normal ingestion does **not** run the standalone `CharacterNamesStage` or its focused
alternative-generation pass. The existing display-name alignment request also classifies
terms; `term_choices.py` chooses one spelling without another model call:

- Ordinary Chinese personal names keep deterministic Pinyin and surname/given-name spacing.
- Recognized foreign transcriptions keep conventional restored spellings.
- Other foreign names, meaningful titles, and semantic terms keep their translated wording.

The first valid choice is stored as a pending `character_name_review`, with one candidate.
Later mentions cannot replace it; readers confirm or correct it through the hovercard.
Nothing is auto-approved or turned into an entity identity. Previously approved glossary
spellings retain precedence, and deleted glossary entries do not resurrect constraints.
Display scanning runs before records extraction, so a records failure cannot prevent
name choices from reaching the reader. Simplified and traditional surname spellings
use the same surname/given-name boundary (龙飞 / 龍飛 → Long Fei); multi-syllable given
names remain joined. Translation instructions apply this spacing on first appearance.

The reader overlays provisional or approved choices on mapped display spans without
rewriting saved translation objects. Later translations use earlier provisional choices
as well as the approved glossary; the exact choices are included in the translation cache
fingerprint. Review status and chapter gates still apply. Chapters translated before a
choice exists retain their saved text; the overlay becomes available once alignment exists.

`character_names.py`, `name_checkpoints.py`, and `refresh_name_reviews.py` retain the
legacy standalone/offline tooling and its saved responses. They are not a normal worker
stage. Do not reintroduce per-passage name inventory or alternative-generation calls into
production; use `display_names.py` / `term_choices.py` for this flow.

## Unlinked reader name cards

`display_scan` links a glossary term to an entity through that entity's aliases in the
novel's **active record generation**, gated on the entity's `first_seen_chapter`; the old
`glossary_binding` ledger and its graph revisions are gone. It then asks the configured
model for literal names in the display text, and exact text matching supplies the offsets.
Newly detected names have no entity id: no glossary approval, entity creation, identity
merging, or records are implied. Capitalization is not the detection rule. This adds one
cached model call per chapter; it can miss or misclassify names.

Spans are published with the chapter, before extraction finishes, and keep the chapter RLS
gate. The frontend can open an empty card without an entity request, and hover mode also
supports clicking. A completed name index is visible on the next chapter load; the reader
does not poll for new mentions while a chapter stays open.

## Existing chapters

With the same environment as the worker, preview a bounded names-only backfill:

```bash
.venv/bin/python -m pipeline.backfill_names --novel NOVEL_UUID --start 1 --end 20
```

Add `--apply` to save it. Only completed chapters are processed. Saved prose, records,
entities and glossary rows are untouched; existing mention links are preserved.
Rerunning is safe and uses the model-result cache. Text changed during discovery aborts
the update for that chapter. This command does not re-run extraction or promote any
provisional glossary mappings.
