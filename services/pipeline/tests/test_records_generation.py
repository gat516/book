from types import SimpleNamespace

import pytest

from pipeline.records_generation import (
    CHECKS_VERSION,
    GenerationFenceError,
    _requested,
    mark_record_processing,
    verify_generation,
)
from pipeline.records_prompts import PROMPT_CONTRACT_VERSION
from pipeline.stages.records import RecordsStage


class _Cursor:
    def __init__(self, *, one=None, many=(), rowcount=1):
        self.one = one
        self.many = list(many)
        self.rowcount = rowcount

    async def fetchone(self):
        return self.one

    async def fetchall(self):
        return self.many


class _DB:
    def __init__(self, rows=(), rowcount=1, conflict=None):
        self.calls = []
        self.rows = list(rows)
        self.rowcount = rowcount
        self.conflict = conflict

    async def execute(self, sql, params=()):
        self.calls.append((sql, params))
        if "FROM entity e" in sql:
            return _Cursor(many=self.rows)
        if "FROM glossary" in sql:
            return _Cursor(many=[])
        if "SELECT status,source_hash FROM record_run" in sql:
            return _Cursor(one=self.conflict)
        return _Cursor(rowcount=self.rowcount)


def _ctx(db):
    return SimpleNamespace(
        db=db,
        novel=SimpleNamespace(id="novel", source_lang="zh", target_lang="en", ontology={"kinds": []}),
    )


@pytest.mark.asyncio
async def test_candidates_use_pinned_generation_without_rereading_active_pointer():
    db = _DB(rows=[("entity", "A", "character")])
    candidates = await RecordsStage()._candidates(
        _ctx(db), 7, [{"name": "A"}], "pinned-generation"
    )
    assert candidates == [{"id": "entity", "canonical": "A", "kind": "character"}]
    entity_call = next(params for sql, params in db.calls if "FROM entity e" in sql)
    assert entity_call[1] == "pinned-generation"
    assert not any("active_record_generation" in sql for sql, _ in db.calls)


@pytest.mark.asyncio
async def test_processing_marker_uses_deterministic_pinned_run():
    db = _DB()
    state = SimpleNamespace(
        record_generation_id="00000000-0000-0000-0000-000000000001",
        record_generation_config={"requested_model": "extract-model"},
        envelope=SimpleNamespace(
            chapter_index=7,
            source_meta=SimpleNamespace(raw_hash="source-hash"),
        ),
    )
    await mark_record_processing(_ctx(db), state)
    sql, params = db.calls[0]
    assert "INSERT INTO record_run" in sql
    assert params[2] == state.record_generation_id
    assert params[3] == 7
    assert params[6] == "extract-model"
    assert "WHERE record_run.status <> 'published'" in sql


@pytest.mark.asyncio
async def test_processing_marker_rejects_immutable_source_change():
    db = _DB(rowcount=0)
    state = SimpleNamespace(
        record_generation_id="00000000-0000-0000-0000-000000000001",
        record_generation_config={"requested_model": "extract-model"},
        envelope=SimpleNamespace(
            chapter_index=7,
            source_meta=SimpleNamespace(raw_hash="new-source-hash"),
        ),
    )
    with pytest.raises(GenerationFenceError, match="input changed"):
        await mark_record_processing(_ctx(db), state)


@pytest.mark.asyncio
async def test_processing_marker_is_noop_for_published_same_source():
    db = _DB(rowcount=0, conflict=("published", "new-source-hash"))
    state = SimpleNamespace(
        record_generation_id="00000000-0000-0000-0000-000000000001",
        record_generation_config={"requested_model": "extract-model"},
        envelope=SimpleNamespace(
            chapter_index=7,
            source_meta=SimpleNamespace(raw_hash="new-source-hash"),
        ),
    )
    assert await mark_record_processing(_ctx(db), state) is False


class _VerifyDB:
    def __init__(self, row):
        self.row = row
        self.calls = []

    async def execute(self, sql, params=()):
        self.calls.append((sql, params))
        return _Cursor(one=self.row)


def _verify_ctx(db):
    return SimpleNamespace(
        db=db,
        novel=SimpleNamespace(id="novel", source_lang="zh", target_lang="en", ontology={"kinds": []}),
        cfg=SimpleNamespace(prompt_version="prompt", llm_model_extract="extract-model"),
        model_override=None,
    )


def test_generation_identity_includes_code_prompt_and_checks_contracts():
    requested = _requested(_verify_ctx(None))
    assert requested.prompt_version == f"prompt:{PROMPT_CONTRACT_VERSION}"
    assert requested.checks_version == CHECKS_VERSION == "records-checks-v2"


@pytest.mark.asyncio
async def test_publication_fence_rejects_switched_active_generation():
    db = _VerifyDB(("other-generation", "active", {"kinds": []}, "extract-model", "prompt", "records-checks-v1", "zh", "en"))
    state = SimpleNamespace(record_generation_id="pinned-generation")
    with pytest.raises(GenerationFenceError, match="generation changed"):
        await verify_generation(_verify_ctx(db), state)
