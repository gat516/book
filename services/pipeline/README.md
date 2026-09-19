# Pipeline runtime

Per chapter: CHUNK → TRANSLATE → DISPLAY_SCAN → FACTS → CHUNK_INDEX
(`pipeline/stages/__init__.py`). Saved chapter prose is published as soon as TRANSLATE
validates it; the rest of the chapter is re-queued as low-priority enrichment behind
untranslated chapters.

## Facts

FACTS (`stages/facts.py`, `.claude/plans/facts-stage.md`) makes one model call per chapter
on the English translation and writes tagged `chapter_fact` rows: short facts, each with a
wiki category and the characters it names, stored as character IDs rather than spellings
(0112). Rows are append-only and keyed by prompt version and source hash, so a re-run of an
unchanged chapter is skipped. `chapter.facts_count` marks the chapter done (0110). The wiki
is assembled from these rows at read time, gated on the fact's chapter (§0).

FACTS reads only its own chapter, so chapters never wait on each other. Operator controls
live in ingest-api (`facts_build.go`): find missing facts, pause, and per-chapter
retry/discard.

## One provisional spelling per term

Normal ingestion does **not** run the standalone `CharacterNamesStage` or its focused
alternative-generation pass. The existing display-name alignment request also classifies
terms; `term_choices.py` chooses one spelling without another model call:

- Chinese personal names take their letters from pypinyin and their spacing from the
  source-names model: "An Ruosu", two-character "Longze Liyue". There is no surname list.
  A name the model translated instead ("Dragon Fei") keeps the matched Pinyin part and
  gets its surname back in Pinyin ("Long Fei"), even when mislabelled as a title.
- Recognized foreign transcriptions keep conventional restored spellings.
- Other foreign names, meaningful titles, and semantic terms keep their translated wording.

The first valid choice is stored as a pending `character_name_review`, with one candidate.
Later mentions cannot replace it; readers confirm or correct it through the hovercard.
Nothing is auto-approved or turned into an entity identity. Previously approved glossary
spellings retain precedence, and deleted glossary entries do not resurrect constraints.
Display scanning runs before FACTS, so a facts failure cannot prevent name choices from
reaching the reader.

The reader overlays provisional or approved choices on mapped display spans without
rewriting saved translation objects. Later translations use earlier provisional choices
as well as the approved glossary; the exact choices are included in the translation cache
fingerprint. Review status and chapter gates still apply. Chapters translated before a
choice exists retain their saved text; the overlay becomes available once alignment exists.

The legacy per-passage name inventory (`character_names.py`, its checkpoints and the
`pinyin_names.py` surname lists) was removed. Do not reintroduce per-passage name inventory
or alternative-generation calls into production; use `source_names.py` /
`display_names.py` / `term_choices.py` for this flow.

## Reader name cards

`display_scan` finds locked glossary terms and pending name choices in the displayed text
by exact search. For prose that arrived translated (bootstrap chapters) it also asks the
configured model for literal names in the display text, and exact text matching supplies
the offsets. Spans carry no identity: no glossary approval or entity is implied.
Capitalization is not the detection rule. This adds one cached model call per such
chapter; it can miss or misclassify names.

Spans are published with the chapter, before FACTS finishes, and keep the chapter RLS
gate. A completed name index is visible on the next chapter load; the reader does not poll
for new mentions while a chapter stays open.

## Existing chapters

With the same environment as the worker, preview a bounded names-only backfill:

```bash
.venv/bin/python -m pipeline.backfill_names --novel NOVEL_UUID --start 1 --end 20
```

Add `--apply` to save it. Only completed chapters are processed. Saved prose, facts and
glossary rows are untouched; existing mention spans are preserved.
Rerunning is safe and uses the model-result cache. Text changed during discovery aborts
the update for that chapter. This command does not re-run extraction or promote any
provisional glossary mappings.
