from unittest.mock import AsyncMock

import pytest

from novel_llm.provider import Completion
from novel_llm.usage import UsageProvider


@pytest.mark.asyncio
async def test_batch_records_each_served_completion(monkeypatch):
    record=AsyncMock()
    monkeypatch.setattr('novel_llm.usage.record',record)
    inner=AsyncMock()
    first=Completion('first','provider','served-model',12,3)
    second=Completion('second','provider','served-model',8,2)
    inner.complete.side_effect=[first,second]
    wrapped=UsageProvider(inner,object(),'book-id')
    batch=await wrapped.batch_submit([
        {'id':'a','prompt':'first request','system':'system'},
        {'id':'b','prompt':'second request','system':'system'}])
    results=await wrapped.batch_poll(batch)
    assert [result['output'] for result in results]==['first','second']
    assert [call.args[-1] for call in record.await_args_list]==[first,second]
    inner.batch_submit.assert_not_called()
