import json

import pytest

from pipeline.passages import (
    FixedOverheadTooLarge,
    PassageTooLarge,
    pack_passages,
)


ROWS = [
    {"id": "p1", "text": "林峰没有杀死赵云。"},
    {"id": "p2", "text": "甲信任乙。"},
    {"id": "p3", "text": "他可能是陈家的人。"},
]


def test_packer_accounts_for_schema_and_output_headroom_without_truncating():
    schema = {"type": "object", "properties": {"claims": {"type": "array"}}}
    batches = pack_passages(
        ROWS,
        instructions="从原文抽取，不得推测。",
        vocabulary={"relations": ["trusts"]},
        schema=schema,
        context_tokens=330,
        output_tokens=20,
    )
    assert [row["id"] for batch in batches for row in batch.passages] == ["p1", "p2", "p3"]
    assert all(batch.request_tokens + batch.output_headroom <= 330 for batch in batches)
    assert all(row["text"] in json.dumps([dict(id=row["id"], text=row["text"])], ensure_ascii=False)
               for row in ROWS)


def test_duplicated_schema_costs_more_than_native_only():
    schema = {"type": "object", "properties": {"claims": {"type": "array", "items": {"type": "string"}}}}
    native = pack_passages(ROWS, instructions="extract", schema=schema,
                           context_tokens=300, output_tokens=10, schema_transport="native")
    duplicated = pack_passages(ROWS, instructions="extract", schema=schema,
                               context_tokens=450, output_tokens=10, schema_transport="duplicated")
    assert duplicated[0].overhead_tokens > native[0].overhead_tokens


def test_fixed_overhead_failure_is_clear():
    with pytest.raises(FixedOverheadTooLarge, match="fixed request overhead"):
        pack_passages([], instructions="x" * 100, schema={"x": "y"},
                      context_tokens=10, output_tokens=5)


def test_one_complete_passage_too_large_is_reported_instead_of_truncated():
    with pytest.raises(PassageTooLarge, match="p1"):
        pack_passages([{"id": "p1", "text": "甲" * 200}], instructions="i",
                      context_tokens=20, output_tokens=5)


def test_packer_flushes_a_fitting_row_into_a_new_batch():
    rows = [{"id": "p1", "text": "x" * 10}, {"id": "p2", "text": "y" * 10}]
    batches = pack_passages(rows, instructions="i", schema={}, context_tokens=55,
                            output_tokens=1, tokenizer=len)

    assert [[row["id"] for row in batch.passages] for batch in batches] == [["p1"], ["p2"]]
    assert all(batch.request_tokens + batch.output_headroom <= 55 for batch in batches)


def test_request_budget_is_separate_from_model_context():
    rows = [{"id": "p1", "text": "a" * 20}, {"id": "p2", "text": "b" * 20}]
    batches = pack_passages(rows, instructions="i", schema={}, context_tokens=1000,
                            request_tokens=80, output_tokens=5, tokenizer=len)

    assert [[row["id"] for row in batch.passages] for batch in batches] == [["p1"], ["p2"]]
    assert all(batch.request_tokens + batch.output_headroom <= 80 for batch in batches)


def test_provider_counter_avoids_byte_overhead_false_rejection():
    # The fake provider tokenizer counts four bytes as one token. The schema is larger
    # than the total request budget in bytes, but its tokenized wire request fits.
    def count(instructions, source, schema, _transport):
        schema_text = json.dumps(schema, ensure_ascii=False)
        return (len(instructions) + len(source) + len(schema_text)) // 4

    batches = pack_passages(
        [{"id": "p1", "text": "甲" * 12}], instructions="i" * 8,
        schema={"properties": {"field": {"enum": ["x" * 80]}}},
        context_tokens=1000, request_tokens=60, output_tokens=5,
        request_counter=count,
    )

    assert len(batches) == 1
    assert batches[0].request_tokens + batches[0].output_headroom <= 60


def test_provider_counter_gets_raw_prompt_in_prompt_schema_mode():
    seen = []

    def count(instructions, source, schema, transport):
        seen.append((instructions, source, schema, transport))
        return 1

    pack_passages([{"id": "p1", "text": "source"}], instructions="extract",
                  schema={"type": "object"}, schema_transport="prompt",
                  context_tokens=100, request_tokens=20, output_tokens=5,
                  request_counter=count)

    assert seen
    assert all("OUTPUT JSON SCHEMA" not in instructions for instructions, *_ in seen)
    assert all(transport == "prompt" for *_, transport in seen)
