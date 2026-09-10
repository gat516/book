import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pipeline.knowledge import KnowledgeEngine
from pipeline.llm.provider import RequestBudgetExceeded


@pytest.mark.asyncio
async def test_hosted_extract_splits_only_after_normalized_request_budget_failure():
    engine = object.__new__(KnowledgeEngine)
    engine.max_concurrent_calls = 1
    engine._call_slots = asyncio.Semaphore(1)
    engine._db_lock = asyncio.Lock()
    engine.extract_limits = {"names": 99, "attributes": 99, "relations": 99, "occurrences": 99}
    engine._extract_context_splits = 0
    engine._extract_subdivisions = 0
    engine._extract_saturation = {key: 0 for key in engine.extract_limits}
    engine._activity = AsyncMock()
    source = "\n".join(("甲来了。" + "旁白" * 75, "乙走了。" + "旁白" * 75,
                           "丙看见丁。" + "旁白" * 75, "戊离开。" + "旁白" * 75))
    offered = []
    empty = {"names": [], "attributes": [], "relations": [], "occurrences": []}

    async def call(_stage, _schema, payload):
        offered.append(tuple(payload["_passage_ids"]))
        if len(offered) == 1:
            raise RequestBudgetExceeded("request exceeds configured context")
        return dict(empty)

    engine.call = call
    result, rejected = await engine._extract_window(source, {}, 0, len(source))
    assert result == empty and rejected == []
    assert engine._extract_context_splits == 1
    assert len(offered) == 3
    assert set(offered[0]) == set(offered[1]) | set(offered[2])
