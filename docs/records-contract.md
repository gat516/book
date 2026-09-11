# Records contract

This is the shared storage and reader contract for the records generation pipeline.
All knowledge rows are scoped by `(novel_id, generation_id, run_id)` and are immutable
after publication. `source_chapter` is the authorization time; `valid_from_chapter` is
story time and never grants access.

`record_generation` pins ontology, prompt/check versions, extraction and rendering
models, and lifecycle state. `novel.active_record_generation` is the only generation
visible to readers. A generation has at most one published `record_run` per chapter.

`record_row` stores the typed record and original index. `record_value` stores source
fields; `record_participant` stores ordered identity-bearing fields. A participant has
exactly one `entity_id` or `record_reference_id`. `record_evidence` points to frozen
`record_passage` rows from the same chapter. `record_rendering` is optional, keyed by
row and field, and never replaces source values. `record_drop` reports parser/check
rejections. `record_mention_binding` is a run-scoped display binding.

Reader endpoints:

* `GET /novels/{id}/chapter/{n}/rows` returns visible rows and `RecordsStatus`.
* `GET /novels/{id}/chapter/{n}/records/status` returns status and bounded diagnostics.
* `POST /novels/{id}/chapter/{n}/records/retry` retries a failed run in place;
  `POST /novels/{id}/chapter/{n}/records/render-retry` retries only rendering;
  `POST /novels/{id}/records/rebuild` creates and activates a fresh generation.
* `GET /novels/{id}/wiki` returns authorized entities plus IDENTITY rows.
* `GET /novels/{id}/timeline` returns EVENT, SPEECH, and PROMISE rows in knowledge
  chapter then passage order.

The opaque status version is derived only from the active generation and runs visible
at the reader chapter. Publishing a future chapter therefore cannot alter an earlier
reader's token.

The JSON shape is intentionally stable: `RecordView.values` is an array of
`{field,source,rendered,render_status}` objects, participants are ordered objects with
`field`, `surface`, and exactly one of `entity_id`/`reference_id`, and evidence is an
array of `{passage_id,quote,text,char_start,char_end,ordinal}`. Rendering may be empty
or failed while `source` remains available.
