# Pipeline runtime and reader records

## Records enrichment

Saved chapter prose is published independently of knowledge enrichment. The records
worker freezes source passages, performs bounded typed discovery, mechanical grounding
checks, and sends one name inventory to who's-who. Only who's-who assignments may bind
an entity; uncertain names remain references. Source values and verbatim evidence are
retained alongside optional offline target-language renderings.

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

Record discovery, identity, and rendering use separate cache identities. Every provider
completion is attributed to its served model before caching. Transport failures retry;
malformed individual records are dropped with bounded diagnostics; a valid empty chapter
is complete. Rendering failure leaves source records available with an explicit fallback.

## Exact source passage references

Source passages are frozen once per run in `record_passage`, with the chapter's
`source_hash`, and every citation points at that frozen row. Passage ids used to be
recomputed from the source's non-blank lines on each read, which meant a re-ingest with
different blank lines silently repointed every stored quote.

The model supplies passage references, never offsets: discovery cites a passage id and
quotes from it, and the checks confirm the quote is verbatim in the cited passage and that
each named participant is written near it. A citation alone never establishes identity --
only who's-who may bind a name to an entity, and an uncertain name stays a
`record_reference` rather than becoming one.

## Call scheduling

A chapter's records work is two provider calls: one bounded typed discovery pass, then one
who's-who inventory. Both go through `ctx.batch_manager`, so per-novel provider
configuration, priority class and the served-model cache key apply exactly as they do to
translation. `GRAPH_MAX_CONCURRENT_CALLS` (default 1) still bounds independent fan-out
elsewhere in a chapter.

The budget binds at the provider, not at the fan-out. Because the shared psycopg
connection is not safe for concurrent cursors, engine writes are serialized by a lock. The
setting is scheduling only: it is deliberately absent from generation identity and the
completion cache key, so it cannot change what a finished call contains.

## Character-name spelling review

Before translation, exact source names are classified for display: ordinary Chinese
personal names use deterministic pinyin; foreign names transcribed in Chinese receive
suggested restored spellings; distinctive personal titles receive meaning-based
translations; named species, groups, places, organizations, objects, and techniques use
semantic translations. A focused second model pass reconsiders ambiguous terms after
the broad inventory pass, so polyphonic Pinyin is not mistaken for foreign-name
restoration. A small conventional-transcription lookup supplements the model for
recognized English names (including complete middle-dot-separated names), so model
misclassification does not reduce those suggestions to pinyin. Restorations/titles always require human approval, even
when there is one choice. They never establish identity or approve facts. Organizations,
places, and other non-person terms are reviewed into the semantic glossary path.

Existing pending pinyin-only suggestions can be refreshed offline using their original
quoted evidence, without later chapters, re-importing, approval, or queue changes:

```bash
cd services/pipeline
../../scripts/with-env.sh .venv/bin/python -m pipeline.refresh_name_reviews \
  --novel-id NOVEL_UUID --source-term 劳伦斯
# Add --apply to save the suggestions. Omit --source-term to refresh all pending names.
```

Already-approved spellings remain locked. Semantic rendering defaults do not override
an existing glossary spelling; use the glossary correction UI for those.

The review UI exposes the classification. Approval maps Chinese/foreign/personal-title
roles to a hard `character_name` spelling constraint and semantic terms to the softer
`semantic_term` constraint. This lets a stale or mistaken review be repaired without
merging identities or approving graph facts.

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
