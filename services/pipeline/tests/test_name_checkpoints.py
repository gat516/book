from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pipeline.llm.provider import AdmissionRejected, Class, Completion
from pipeline.name_checkpoints import NameCheckpoints
from pipeline.stages.character_names import _discover
from test_character_names import context, item


class CheckpointDB:
    autocommit = True
    info = SimpleNamespace(transaction_status=0)

    def __init__(self):
        self.rows = {}

    async def execute(self, sql, params):
        if sql.startswith('SELECT'):
            return SimpleNamespace(fetchone=AsyncMock(return_value=self.rows.get(params)))
        novel, chapter, key, _, _, _, provider, model, response = params
        self.rows.setdefault((novel, chapter, key), (response, provider, model))


def setup(items):
    ctx = context(items)
    ctx.db = CheckpointDB()
    ctx.provider_id = 'ollama'
    ctx.novel.source_lang = 'zh'
    original = ctx.provider.complete.side_effect

    async def complete(prompt, **kwargs):
        result = await original(prompt, **kwargs)
        return Completion(result.text, 'actual-provider', 'actual-model')

    ctx.provider.complete = AsyncMock(side_effect=complete)
    return ctx


def state(source, chapter=1):
    return SimpleNamespace(envelope=SimpleNamespace(raw_text=source, chapter_index=chapter))


@pytest.mark.parametrize('focused', [False, True])
async def test_rejection_resumes_completed_calls(monkeypatch, focused):
    from pipeline.stages import character_names
    source = '索拉文走进大厅。'
    ctx = setup([item('索拉文', 'chinese_personal')] if focused else [])
    if not focused:
        monkeypatch.setattr(character_names, '_batches', lambda source: iter([
            [{'id': 'p1', 'text': '一。'}], [{'id': 'p2', 'text': '二。'}]]))
    original = ctx.provider.complete.side_effect
    calls = 0

    async def fail_second(prompt, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise AdmissionRejected()
        return await original(prompt, **kwargs)

    ctx.provider.complete.side_effect = fail_second
    with pytest.raises(AdmissionRejected):
        await _discover(ctx, source, complete=NameCheckpoints(ctx, state(source)))
    assert len(ctx.db.rows) == 1
    # A new helper/provider must resume using only persisted rows.
    ctx.provider.complete = AsyncMock(side_effect=original)
    await _discover(ctx, source, complete=NameCheckpoints(ctx, state(source)))
    assert ctx.provider.complete.await_count == 1
    assert ('ambiguous_terms' if focused else 'p2') in ctx.provider.complete.call_args.args[0]
    assert len(ctx.db.rows) == 2
    assert all(row[1:] == ('actual-provider', 'actual-model') for row in ctx.db.rows.values())
    ctx.provider.complete.reset_mock()
    await _discover(ctx, source, complete=NameCheckpoints(ctx, state(source)))
    ctx.provider.complete.assert_not_awaited()


@pytest.mark.parametrize('change', ['source', 'chapter', 'novel', 'language', 'provider', 'model', 'prompt', 'schema'])
async def test_changed_inputs_do_not_reuse_checkpoint(change):
    ctx = setup([])
    current = state('一。')
    kwargs = dict(system='inventory', json_mode=True, json_schema={}, cls=Class.BATCH, model='model-a')
    prompt = 'request'
    ctx.provider.complete = AsyncMock(return_value=Completion('{}', 'served', 'model'))
    async with NameCheckpoints(ctx, current)(ctx, prompt, **kwargs):
        pass
    if change == 'source': current = state('二。')
    if change == 'chapter': current = state('一。', 2)
    if change == 'novel': ctx.novel.id = 'other'
    if change == 'language': ctx.novel.target_lang = 'ja'
    if change == 'provider': ctx.provider_id = 'custom'
    if change == 'model': kwargs['model'] = 'model-b'
    if change == 'prompt': prompt = 'changed'
    if change == 'schema': kwargs['json_schema'] = {'type': 'object'}
    async with NameCheckpoints(ctx, current)(ctx, prompt, **kwargs):
        pass
    assert ctx.provider.complete.await_count == 2


async def test_invalid_discovery_response_is_not_checkpointed():
    ctx = setup([])
    ctx.provider.complete = AsyncMock(return_value=Completion('{"reviewed": false, "names": []}', 'p', 'm'))
    with pytest.raises(ValueError):
        await _discover(ctx, '一。', complete=NameCheckpoints(ctx, state('一。')))
    assert not ctx.db.rows


async def test_outer_transaction_is_rejected():
    ctx = setup([])
    ctx.db.autocommit = False
    with pytest.raises(RuntimeError, match='autocommit'):
        await _discover(ctx, '一。', complete=NameCheckpoints(ctx, state('一。')))
    ctx.provider.complete.assert_not_awaited()


@pytest.mark.db
async def test_checkpoint_survives_connection_restart(db_conn):
    import psycopg
    from conftest import DATABASE_URL
    from fixtures import make_novel, delete_novel

    novel_id = await make_novel(db_conn)
    ctx = setup([])
    ctx.novel.id = novel_id
    ctx.db = db_conn
    source = '无人走进大厅。'
    try:
        await _discover(ctx, source, complete=NameCheckpoints(ctx, state(source)))
        async with await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True) as restarted:
            ctx.db = restarted
            ctx.provider.complete = AsyncMock(side_effect=AssertionError('completed call was repeated'))
            assert await _discover(ctx, source, complete=NameCheckpoints(ctx, state(source))) == {}
            # Reader roles have no access to raw checkpoint responses (§0).
            row = await (await restarted.execute(
                "SELECT has_table_privilege('rls_reader', 'character_name_checkpoint', 'SELECT')"
            )).fetchone()
            assert row == (False,)
    finally:
        await delete_novel(db_conn, novel_id)
    assert await (await db_conn.execute(
        'SELECT 1 FROM character_name_checkpoint WHERE novel_id=%s', (novel_id,))).fetchone() is None


async def test_invalid_focused_response_keeps_only_discovery_checkpoint():
    ctx = setup([item('索拉文', 'chinese_personal')])
    original = ctx.provider.complete.side_effect

    async def invalid_focused(prompt, **kwargs):
        if 'ambiguous_terms' in prompt:
            return Completion('{"reviewed": true, "decisions": []}', 'p', 'm')
        return await original(prompt, **kwargs)

    ctx.provider.complete.side_effect = invalid_focused
    source = '索拉文走进大厅。'
    plans = await _discover(ctx, source, complete=NameCheckpoints(ctx, state(source)))
    assert '索拉文' in plans
    assert len(ctx.db.rows) == 1
