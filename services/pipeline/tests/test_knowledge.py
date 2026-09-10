"""Evidence, occurrence identity, revision publication and spoiler regressions."""
import json
from uuid import uuid4
from types import SimpleNamespace

import pytest
from psycopg.types.json import Jsonb

from pipeline.context import PipelineState
from pipeline.envelope import ChapterEnvelope
from pipeline.evidence import (Names, Proposals, IdentityDecisions, ExtractProposals, Decision,
    Verification, Alignments, source_mentions,
    validate_proposals, aligned_mentions, approved, digest, stable_id, passage, PROMPT_VERSION)
from pipeline.passages import PassageContract, source_passages
from pipeline.graph_rebuild import next_retryable_active_revision,graph_retry_delay_minutes
from pipeline.graph_rebuild import promote_verified_glossary
from pipeline.knowledge import KnowledgeEngine
from tests.fixtures import make_novel
from novel_llm.provider import Completion

ONTOLOGY=dict(kinds=['character','place','group'],relations=['member_of'],attributes=[dict(name='description',kinds=['character','place','group'])])


def _cache_test_revision(novel, *, served_model='claude-concrete'):
    return dict(id=str(uuid4()), novel_id=str(novel), ontology=ONTOLOGY,
                prompt_version=PROMPT_VERSION,
                model=dict(provider='anthropic', name='claude-alias',
                            served_provider='anthropic', served_model=served_model))


def _cache_test_response(surface='凌峰', served_model='claude-concrete', source=None):
    source = source or f'{surface}来了。'
    return Completion(text=json.dumps(dict(
        names=[dict(surface=surface, kind='character',
                    passage_id=source_passages(source)[0]['id'])],
        attributes=[],relations=[],occurrences=[])),
        served_provider='anthropic', served_model=served_model)


async def _cache_test_call(db, cfg, revision, provider, source='凌峰来了。'):
    # The argument is deliberately the completion mock, never a provider. AsyncMock
    # dynamically manufactures arbitrary attributes, so feature-detecting ``complete``
    # would mistake a bare mock for a provider and produce an unconfigured child mock.
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    provider = SimpleNamespace(complete=provider, aclose=AsyncMock())
    engine = KnowledgeEngine(db, cfg, revision, provider=provider)
    try:
        return await engine.call('extract', ExtractProposals, dict(
            source=source, ontology=ONTOLOGY, _passage_ids=[source_passages(source)[0]['id']]))
    finally:
        await engine.close()


@pytest.mark.db
async def test_completion_cache_reuses_identical_request_across_revisions(db_conn, monkeypatch):
    from unittest.mock import AsyncMock
    from pipeline.config import Config
    monkeypatch.setenv('GRAPH_OLLAMA_FIRST_TOKEN_SECONDS', '120')
    monkeypatch.setenv('GRAPH_OLLAMA_TIMEOUT_SECONDS', '120')
    cfg = Config.load()
    async with db_conn.transaction(force_rollback=True):
        novel = await make_novel(db_conn, ontology=json.dumps(ONTOLOGY))
        first = AsyncMock(return_value=_cache_test_response())
        await _cache_test_call(db_conn, cfg, _cache_test_revision(novel), first)
        second = AsyncMock(return_value=_cache_test_response())
        result = await _cache_test_call(db_conn, cfg, _cache_test_revision(novel), second)
        assert result['names'][0]['surface'] == '凌峰'
        second.assert_not_awaited()

        changed = AsyncMock(return_value=_cache_test_response(source='凌峰离开了。'))
        await _cache_test_call(db_conn, cfg, _cache_test_revision(novel), changed,
                               source='凌峰离开了。')
        changed.assert_awaited_once()


@pytest.mark.db
async def test_completion_cache_requires_explicit_served_pin_and_identity_match(db_conn, monkeypatch):
    from unittest.mock import AsyncMock
    from pipeline.config import Config
    monkeypatch.setenv('GRAPH_OLLAMA_FIRST_TOKEN_SECONDS', '120')
    monkeypatch.setenv('GRAPH_OLLAMA_TIMEOUT_SECONDS', '120')
    cfg = Config.load()
    async with db_conn.transaction(force_rollback=True):
        novel = await make_novel(db_conn, ontology=json.dumps(ONTOLOGY))
        first = AsyncMock(return_value=_cache_test_response())
        await _cache_test_call(db_conn, cfg, _cache_test_revision(novel), first)

        # Same requested alias and request, but a changed concrete pin cannot select
        # the old row and must call the provider for the new identity.
        changed_pin = AsyncMock(return_value=_cache_test_response(served_model='claude-new'))
        await _cache_test_call(db_conn, cfg,
                               _cache_test_revision(novel, served_model='claude-new'),
                               changed_pin)
        changed_pin.assert_awaited_once()

        # Without an explicit pin, a concrete response is rejected rather than being
        # treated as an arbitrary alias fallback.
        drift = AsyncMock(return_value=_cache_test_response())
        unpinned = _cache_test_revision(novel)
        unpinned['model'].pop('served_provider')
        unpinned['model'].pop('served_model')
        with pytest.raises(RuntimeError, match='serving identity changed'):
            await _cache_test_call(db_conn, cfg, unpinned, drift)


@pytest.mark.db
async def test_completion_cache_refreshes_legacy_empty_provenance_row(db_conn, monkeypatch):
    from unittest.mock import AsyncMock
    from pipeline.config import Config
    monkeypatch.setenv('GRAPH_OLLAMA_FIRST_TOKEN_SECONDS', '120')
    monkeypatch.setenv('GRAPH_OLLAMA_TIMEOUT_SECONDS', '120')
    cfg = Config.load()
    async with db_conn.transaction(force_rollback=True):
        novel = await make_novel(db_conn, ontology=json.dumps(ONTOLOGY))
        revision = _cache_test_revision(novel)
        first = AsyncMock(return_value=_cache_test_response())
        await _cache_test_call(db_conn, cfg, revision, first)
        row = await (await db_conn.execute(
            "SELECT cache_key,created_at FROM completion_cache WHERE novel_id=%s",
            (novel,))).fetchone()
        await db_conn.execute('''UPDATE completion_cache SET requested_provider='',
            requested_model='',stage_prompt_version='',prompt_digest='',schema_digest=''
            WHERE novel_id=%s''', (novel,))

        refresh = AsyncMock(return_value=_cache_test_response())
        await _cache_test_call(db_conn, cfg, revision, refresh)
        refresh.assert_awaited_once()
        refreshed = await (await db_conn.execute('''SELECT requested_provider,
            requested_model,stage_prompt_version,prompt_digest,schema_digest,created_at
            FROM completion_cache WHERE novel_id=%s AND cache_key=%s''',
            (novel, row[0]))).fetchone()
        assert refreshed[0:3] == ('anthropic', 'claude-alias',
                                  'evidence-v21-merged-extract')
        assert refreshed[3] and refreshed[4]
        assert refreshed[5] == row[1]
        hit = AsyncMock()
        await _cache_test_call(db_conn, cfg, revision, hit)
        hit.assert_not_awaited()


async def test_graph_uses_configured_runtime_and_keeps_deadlines_out_of_identity(monkeypatch):
    from dataclasses import replace
    from pipeline.config import Config, graph_runtime
    monkeypatch.setenv('GRAPH_OLLAMA_FIRST_TOKEN_SECONDS','900')
    monkeypatch.setenv('GRAPH_OLLAMA_TIMEOUT_SECONDS','30')
    # Pinned too: this test asserts 1800 below, and inheriting a real deployment's tuned
    # deadline from .env made it fail the moment that value was raised.
    monkeypatch.setenv('GRAPH_OLLAMA_TOTAL_TIMEOUT_SECONDS','1800')
    cfg=Config.load()
    runtime=graph_runtime(cfg)
    engine=KnowledgeEngine(None,cfg,dict(id='test',model=dict(provider='ollama',name='test')))
    try:
        assert engine.provider._client.timeout.read==1800
        assert engine.provider._first_token_timeout==900 and engine.provider._idle_timeout==30
        assert engine.provider._total_timeout==1800 and engine.provider._stream
        assert runtime['identity'] == graph_runtime(replace(cfg,graph_ollama_total_timeout_seconds=2400))['identity']
    finally:
        await engine.close()
    with pytest.raises(ValueError,match='timeouts'):
        graph_runtime(replace(cfg,graph_ollama_total_timeout_seconds=float('inf')))
    with pytest.raises(ValueError,match='output budget'):
        graph_runtime(replace(cfg,graph_ollama_num_predict=0))
    with pytest.raises(ValueError,match='request budget'):
        graph_runtime(replace(cfg,hosted_graph_request_tokens=cfg.hosted_graph_output_tokens))


def test_request_token_counter_adapter_keeps_provider_wire_ownership():
    calls = []
    engine = object.__new__(KnowledgeEngine)
    engine.model = 'model'
    engine.provider = SimpleNamespace(count_request_tokens=lambda **kwargs: calls.append(kwargs) or 7)

    assert engine._request_token_counter()('instructions', '{"passages":[]}',
                                           {'type': 'object'}, 'prompt') == 7
    assert calls == [dict(prompt='instructions{"passages":[]}', system='',
                          json_schema={'type': 'object'}, model='model')]


async def test_discover_num_ctx_reads_back_what_ollama_actually_loaded(monkeypatch):
    """§ dynamic context sizing: the window comes from a load probe, not from config.

    POST /api/generate with no prompt loads without generating (Ollama's own contract);
    GET /api/ps then reports the context length that host actually resident it with.
    """
    import httpx
    from pipeline.config import Config
    from pipeline.graph_rebuild import discover_num_ctx
    seen = []
    def handle(request):
        seen.append((request.method, request.url.path))
        if request.url.path == '/api/generate':
            return httpx.Response(200, json={'model': 'qwen3', 'done': True})
        return httpx.Response(200, json={'models': [
            {'name': 'qwen3', 'context_length': 8192},
            {'name': 'other-model', 'context_length': 4096},
        ]})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handle)))
    num_ctx = await discover_num_ctx(Config.load(), 'qwen3')
    assert num_ctx == 8192
    assert seen == [('POST', '/api/generate'), ('GET', '/api/ps')]


async def test_discover_num_ctx_refuses_a_model_that_never_became_resident(monkeypatch):
    import httpx
    from pipeline.config import Config
    from pipeline.graph_rebuild import discover_num_ctx
    def handle(request):
        if request.url.path == '/api/generate':
            return httpx.Response(200, json={'model': 'qwen3', 'done': True})
        return httpx.Response(200, json={'models': []})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handle)))
    with pytest.raises(ValueError, match='resident context length'):
        await discover_num_ctx(Config.load(), 'qwen3')


def test_without_num_ctx_strips_only_num_ctx():
    from pipeline.graph_rebuild import _without_num_ctx
    model = dict(provider='ollama', name='m', digest='d',
                 identity=dict(version='v', stream=True, num_ctx=16384, num_predict=4096))
    stripped = _without_num_ctx(model)
    assert stripped == dict(model, identity=dict(version='v', stream=True, num_predict=4096))
    # A model with no identity, or an identity with no num_ctx, passes through unchanged --
    # both are real shapes local_model() returns depending on caller.
    assert _without_num_ctx(dict(provider='ollama', name='m')) == dict(provider='ollama', name='m')
    assert _without_num_ctx(dict(model, identity=dict(version='v'))) == dict(model, identity=dict(version='v'))


@pytest.mark.db
async def test_prepare_pins_discovered_num_ctx_and_resume_tolerates_it_drifting(db_conn, monkeypatch):
    """The regression this whole feature is for: cj-desktop's available VRAM (and so its
    own vram-based num_ctx) can legitimately differ between prepare and a later resume
    without anything about the pinned model actually changing. Only a real change --
    here, the digest -- may still refuse to resume."""
    from unittest.mock import AsyncMock
    from novel_llm import AdmissionRejected
    from pipeline import graph_rebuild
    from pipeline.config import Config

    cfg = Config.load()
    ontology = {"kinds": ["character"], "attributes": [], "relations": []}
    base_identity = {"provider": "ollama", "name": "test", "digest": "digest-1",
                      "identity": {"num_predict": 4096}}
    monkeypatch.setattr(graph_rebuild, "objects", lambda _cfg: None)
    monkeypatch.setattr(graph_rebuild, "read_object", lambda *_args: "source")

    async with db_conn.transaction(force_rollback=True):
        novel = await make_novel(db_conn, ontology=json.dumps(ontology))
        await db_conn.execute('''INSERT INTO chapter
            (novel_id,chapter_index,raw_hash,raw_uri,translated_uri,source_meta,status,translation_ready)
            VALUES(%s,1,'raw-1','raw-1','raw-1','{}','done',true)''', (novel,))

        monkeypatch.setattr(graph_rebuild, "local_model", AsyncMock(return_value=base_identity))
        monkeypatch.setattr(graph_rebuild, "discover_num_ctx", AsyncMock(return_value=16384))
        rid = await graph_rebuild.prepare(db_conn, cfg, novel, "test")

        pinned = (await (await db_conn.execute(
            "SELECT model FROM graph_revision WHERE id=%s", (rid,))).fetchone())[0]
        assert pinned["identity"]["num_ctx"] == 16384

        # A later resume sees a different num_ctx (VRAM shifted) but the same model --
        # the preamble must not refuse to run.
        drifted_ctx_identity = dict(base_identity, identity=dict(base_identity["identity"], num_ctx=8192))
        monkeypatch.setattr(graph_rebuild, "local_model", AsyncMock(return_value=drifted_ctx_identity))
        monkeypatch.setattr(KnowledgeEngine, "extract", AsyncMock(side_effect=AdmissionRejected()))
        with pytest.raises(AdmissionRejected):
            await graph_rebuild.resume(db_conn, cfg, rid, limit=1)

        # A genuine model change (different digest) must still refuse.
        changed_digest = dict(base_identity, digest="digest-2",
                              identity=dict(base_identity["identity"], num_ctx=8192))
        monkeypatch.setattr(graph_rebuild, "local_model", AsyncMock(return_value=changed_digest))
        with pytest.raises(ValueError, match="model or inference configuration changed"):
            await graph_rebuild.resume(db_conn, cfg, rid, limit=1)


@pytest.mark.db
async def test_graph_terminal_provider_failure_blocks_knowledge_run(db_conn, monkeypatch):
    """A terminal provider class is durable on the chapter run, without provider text."""
    from unittest.mock import AsyncMock
    from novel_llm import AdmissionRejected
    from pipeline import graph_rebuild
    from pipeline.config import Config

    cfg = Config.load()
    identity = {"provider": "ollama", "name": "test", "digest": "digest-1",
                "identity": {"num_predict": 4096}}
    monkeypatch.setattr(graph_rebuild, "objects", lambda _cfg: None)
    monkeypatch.setattr(graph_rebuild, "read_object", lambda *_args: "source")
    monkeypatch.setattr(graph_rebuild, "local_model", AsyncMock(return_value=identity))
    monkeypatch.setattr(graph_rebuild, "discover_num_ctx", AsyncMock(return_value=16384))

    async with db_conn.transaction(force_rollback=True):
        novel = await make_novel(db_conn, ontology=json.dumps({
            "kinds": ["character"], "attributes": [], "relations": []}))
        await db_conn.execute('''INSERT INTO chapter
            (novel_id,chapter_index,raw_hash,raw_uri,translated_uri,source_meta,status,translation_ready)
            VALUES(%s,1,'raw-1','raw-1','raw-1','{}','done',true)''', (novel,))
        rid = await graph_rebuild.prepare(db_conn, cfg, novel, "test")

        async def terminal(self, novel_id, chapter, source, display, target_lang):
            await self._ensure_run(novel_id, chapter, source, display)
            raise AdmissionRejected("quota", category="quota_exhausted")

        monkeypatch.setattr(graph_rebuild.KnowledgeEngine, "extract", terminal)
        with pytest.raises(AdmissionRejected):
            await graph_rebuild.resume(db_conn, cfg, rid, limit=1)

        row = await (await db_conn.execute(
            "SELECT state,blocked_category,blocked_at,error FROM chapter_knowledge_run "
            "WHERE revision_id=%s AND chapter_index=1", (rid,))).fetchone()
        assert row[0] == "failed"
        assert row[1] == "quota_exhausted"
        assert row[2] is not None
        assert row[3] == "quota_exhausted"


async def test_runtime_preflight_never_generates_or_loads_models(monkeypatch):
    import httpx
    from pipeline.config import Config
    from pipeline.inference_runtime import preflight
    original=httpx.AsyncClient
    seen=[]
    def handle(request):
        seen.append((request.method,request.url.path))
        bodies={'/api/tags':dict(models=[dict(name='qwen2.5:7b-instruct',digest='installed')]),
                '/api/version':dict(version='test'),'/api/ps':dict(models=[])}
        return httpx.Response(200,json=bodies[request.url.path])
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:original(**kwargs,transport=httpx.MockTransport(handle)))
    report=await preflight(Config.load(),'qwen2.5:7b-instruct')
    assert report['inference_started'] is False
    assert seen==[('GET','/api/tags'),('GET','/api/version'),('GET','/api/ps')]


@pytest.mark.db
async def test_graph_cache_preserves_runtime_metrics_and_avoids_repeat_inference(db_conn):
    from unittest.mock import AsyncMock
    from pipeline.config import Config
    from novel_llm.provider import Completion
    async with db_conn.transaction(force_rollback=True):
        novel=await make_novel(db_conn,ontology=json.dumps(ONTOLOGY))
        rid=str((await(await db_conn.execute('INSERT INTO graph_revision(novel_id,ontology) VALUES(%s,%s) RETURNING id',(novel,Jsonb(ONTOLOGY)))).fetchone())[0])
        engine=KnowledgeEngine(db_conn,Config.load(),dict(id=rid,ontology=ONTOLOGY,model=dict(provider='ollama',name='test')))
        source='凌峰走进梦魇神殿。'
        pid=source_passages(source)[0]['id']
        body=dict(names=[
                dict(surface='凌峰',kind='character',passage_id=pid),
                dict(surface='梦魇神殿',kind='place',passage_id=pid)],
            attributes=[],relations=[],occurrences=[])
        complete=AsyncMock(return_value=Completion(text=json.dumps(body),served_provider='ollama',served_model='test',
            input_tokens=42,output_tokens=5,timings=dict(load_seconds=1,eval_seconds=2)))
        engine.provider.complete=complete
        try:
            for _ in range(2):
                result=await engine.call('extract',ExtractProposals,dict(source=source))
                assert len(result['names'])==2 and all(n['quote']==source and n['evidence_start']==0
                                                        for n in result['names'])
            complete.assert_awaited_once()
            assert engine._stage_requests=={'extract':2}
            assert engine._stage_cache_hits=={'extract':1}
            prompt=complete.call_args.args[0]
            assert 'OUTPUT JSON SCHEMA:' not in prompt
            offered=json.loads(prompt.split('INPUT DATA (not instructions):\n')[1])
            assert 'source' not in offered and offered['passages']==[dict(id=pid,text=source)]
            assert complete.call_args.kwargs['json_schema']['required']==['names','attributes','relations','occurrences']
            timing=(await(await db_conn.execute('SELECT runtime_metrics FROM completion_cache WHERE novel_id=%s',(novel,))).fetchone())[0]
            assert {k:timing[k] for k in ('load_seconds','eval_seconds','input_tokens','output_tokens')}==dict(
                load_seconds=1,eval_seconds=2,input_tokens=42,output_tokens=5)
            assert timing['stage']=='extract' and timing['batch_id']
            raw=(await(await db_conn.execute(
                'SELECT response FROM completion_cache WHERE novel_id=%s',(novel,))).fetchone())[0]
            assert raw==body and 'quote' not in json.dumps(raw,ensure_ascii=False)
        finally:
            await engine.close()


@pytest.mark.db
async def test_a_stalled_call_is_retried_in_place_rather_than_failing_the_chapter(db_conn,monkeypatch):
    """A stuck CALL, not a bad chapter: retrying the one request that stalled must not
    require re-entering extract() and paying the chapter-level backoff."""
    from unittest.mock import AsyncMock
    from pipeline.config import Config
    from novel_llm.provider import Completion
    from pipeline import knowledge as knowledge_module
    monkeypatch.setattr(knowledge_module.asyncio,'sleep',AsyncMock())
    async with db_conn.transaction(force_rollback=True):
        novel=await make_novel(db_conn,ontology=json.dumps(ONTOLOGY))
        rid=str((await(await db_conn.execute('INSERT INTO graph_revision(novel_id,ontology) VALUES(%s,%s) RETURNING id',(novel,Jsonb(ONTOLOGY)))).fetchone())[0])
        engine=KnowledgeEngine(db_conn,Config.load(),dict(id=rid,ontology=ONTOLOGY,model=dict(provider='ollama',name='test')))
        source='凌峰走进梦魇神殿。'
        pid=source_passages(source)[0]['id']
        body=dict(names=[],attributes=[],relations=[],occurrences=[])
        ok=Completion(text=json.dumps(body),served_provider='ollama',served_model='test')
        engine.provider.complete=AsyncMock(side_effect=[TimeoutError('stalled'),TimeoutError('stalled'),ok])
        engine.provider.last_stream_diagnostics={}
        try:
            result=await engine.call('extract',ExtractProposals,dict(source=source))
            assert result['names']==[]
            assert engine.provider.complete.await_count==3
            assert knowledge_module.asyncio.sleep.await_count==2
        finally:
            await engine.close()


@pytest.mark.db
async def test_a_stalled_call_still_fails_the_chapter_once_retries_are_exhausted(db_conn,monkeypatch):
    from unittest.mock import AsyncMock
    from pipeline.config import Config
    from pipeline import knowledge as knowledge_module
    monkeypatch.setattr(knowledge_module.asyncio,'sleep',AsyncMock())
    async with db_conn.transaction(force_rollback=True):
        novel=await make_novel(db_conn,ontology=json.dumps(ONTOLOGY))
        rid=str((await(await db_conn.execute('INSERT INTO graph_revision(novel_id,ontology) VALUES(%s,%s) RETURNING id',(novel,Jsonb(ONTOLOGY)))).fetchone())[0])
        engine=KnowledgeEngine(db_conn,Config.load(),dict(id=rid,ontology=ONTOLOGY,model=dict(provider='ollama',name='test')))
        source='凌峰走进梦魇神殿。'
        engine.provider.complete=AsyncMock(side_effect=TimeoutError('stalled'))
        engine.provider.last_stream_diagnostics={}
        try:
            with pytest.raises(TimeoutError):
                await engine.call('extract',ExtractProposals,dict(source=source))
            assert engine.provider.complete.await_count==knowledge_module.STALL_RETRY_ATTEMPTS+1
        finally:
            await engine.close()


@pytest.mark.db
async def test_graph_runtime_backpressure_keeps_job_resumable(db_conn,monkeypatch):
    from unittest.mock import AsyncMock
    from pipeline.config import Config
    from pipeline import graph_rebuild
    from novel_llm import AdmissionRejected
    identity=dict(provider='ollama',name='test')
    monkeypatch.setattr(graph_rebuild,'local_model',AsyncMock(return_value=identity))
    monkeypatch.setattr(graph_rebuild,'objects',lambda cfg:None)
    monkeypatch.setattr(graph_rebuild,'read_object',lambda *args:'source')
    monkeypatch.setattr(KnowledgeEngine,'extract',AsyncMock(side_effect=AdmissionRejected()))
    async with db_conn.transaction(force_rollback=True):
        novel=await make_novel(db_conn,ontology=json.dumps(ONTOLOGY))
        snapshot=dict(chapters=[dict(chapter=1,raw_uri='saved',translated_uri=None,
                                    source_hash=digest('source'),display_hash=digest('source'))])
        rid=str((await(await db_conn.execute('INSERT INTO graph_revision(novel_id,ontology,model,snapshot,prompt_version) VALUES(%s,%s,%s,%s,%s) RETURNING id',
            (novel,Jsonb(ONTOLOGY),Jsonb(identity),Jsonb(snapshot),PROMPT_VERSION))).fetchone())[0])
        await db_conn.execute('INSERT INTO graph_job(revision_id,chapter_index,input_hash,model_identity,generation) VALUES(%s,1,%s,%s,1)',(rid,'input','model'))
        with pytest.raises(AdmissionRejected):
            await graph_rebuild.resume(db_conn,Config.load(),rid)
        assert (await(await db_conn.execute('SELECT state,error FROM graph_job WHERE revision_id=%s',(rid,))).fetchone())==('pending',None)


def test_passage_ids_preserve_source_bytes_offsets_and_repeated_context():
    source='😀\r\n凌峰说道：“走！”\n\n凌峰说道：“走！”\n'+'字'*390+'很长的名字'*10+'字'*100
    ps=source_passages(source)
    assert ps==source_passages(source)
    assert all(source[p['char_start']:p['char_end']]==p['text'] for p in ps)
    assert all(len(p['text'])<=400 for p in ps)
    assert ps[1]['text']==ps[2]['text'] and ps[1]['id']!=ps[2]['id']
    c=PassageContract(source)
    ev=c.resolve(ps[2]['id'])
    assert passage(source,ev['quote']) is None  # ambiguous without exact offset
    assert passage(source,ev['quote'],start=ev['evidence_start'])['char_start']==ps[2]['char_start']
    assert PassageContract(source+'changed').resolve(ps[2]['id']) is None
    c.by_id[ps[2]['id']]['text']='rewritten'
    assert c.resolve(ps[2]['id']) is None


def test_name_contract_requires_all_kinds_and_exact_containing_passage():
    c=PassageContract('凌峰走进梦魇神殿。\n啸牙冒险团到来。')
    first,second=c.passages
    schema=c.schema('extract',ExtractProposals,ONTOLOGY)
    assert schema['properties']['names']['items']['properties']['kind']['enum']==ONTOLOGY['kinds']
    assert 'quote' not in json.dumps(schema)
    body=dict(names=[
        dict(surface='凌峰',kind='character',passage_id=first['id']),
        dict(surface='梦魇神殿',kind='place',passage_id=first['id']),
        dict(surface='啸牙冒险团',kind='group',passage_id=second['id'])],
        attributes=[],relations=[],occurrences=[])
    result=c.materialize('extract',ExtractProposals,body,ONTOLOGY)
    assert {n['kind'] for n in result['names']}==set(ONTOLOGY['kinds'])
    assert result['names'][0]['quote']==first['text']
    with pytest.raises(ValueError,match='surface absent'):
        c.materialize('extract',ExtractProposals,dict(body,names=[
            dict(surface='虚构组织',kind='group',passage_id=first['id'])]),ONTOLOGY)


def test_extract_wire_schema_is_bounded_and_never_carries_quotes():
    source='凌峰来了。\n他成为首领。';c=PassageContract(source)
    extract=c.schema('extract',ExtractProposals,ONTOLOGY)
    assert set(extract['properties'])=={'names','attributes','relations','occurrences'}
    item=extract['properties']['attributes']['items']
    assert item['properties']['value']['maxLength']==400
    assert item['properties']['passage_ids']['maxItems']==2
    assert 'quote' not in json.dumps(extract)


def _homonym_engine(decide):
    """A KnowledgeEngine stub whose identity selector is `decide` and whose verifier passes."""
    from pipeline.evidence import IdentityDecisions as _ID, Verification as _V
    engine=object.__new__(KnowledgeEngine)
    engine.alignment_limit=32
    engine.identity_batch_size=24
    engine.identity_soft_bytes=24*1024
    engine.identity_hard_bytes=32*1024
    async def call(stage,_schema,payload):
        if stage=='identity_slots':
            return _ID(decisions=decide(payload['identity_occurrences']))
        return _V(verdicts=[dict(id=item['item_ref'],supported=True,reason='supported')
                            for item in payload['items']])
    engine.call=call
    return engine


async def test_same_spelling_occurrences_can_remain_distinct_new_identities():
    """Two 青山 in one chapter are two subjects unless coreference is established (§0)."""
    source='青山来了。\n另一个青山离开。'
    ms=source_mentions('book',1,source,Names(names=[
        dict(surface='青山',kind='character',quote=source,named=True)]),ONTOLOGY)
    assert len(ms)==2
    engine=_homonym_engine(lambda rows:[dict(occurrence_ref=row['occurrence_ref'],
        outcome='new',target_ref=row['occurrence_ref'],reason_code='new_first_appearance',
        explanation='explicitly another person',quote=row['subject']['surface'],
        evidence_start=row['subject']['char_start']) for row in rows])
    verified,rejected,count=await engine._resolve_incremental(
        source,ms,{m['id']:[] for m in ms},ONTOLOGY)
    assert not rejected and count==2
    # Each occurrence roots itself: the surfaces were never merged on spelling alone.
    assert [item['target_id'] for item in verified]==[m['id'] for m in ms]


async def test_empty_candidates_allow_new_self_root():
    """No earlier entity is not a reason to withhold a supported first appearance."""
    source='凌峰来了。'
    ms=source_mentions('book',1,source,Names(names=[
        dict(surface='凌峰',kind='character',quote=source,named=True)]),ONTOLOGY)
    offered={}
    def decide(rows):
        offered.update(rows[0]['choices'])
        return [dict(occurrence_ref='o1',outcome='new',target_ref='o1',
            reason_code='new_first_appearance',explanation='Explicitly named first appearance.',
            quote=source,evidence_start=0)]
    engine=_homonym_engine(decide)
    verified,rejected,count=await engine._resolve_incremental(
        source,ms,{ms[0]['id']:[]},ONTOLOGY)
    # An empty candidate set still offers new:self, and never a durable ID on the wire.
    assert set(offered)=={'new:self','unresolved'}
    assert not rejected and count==1
    assert verified[0]['mention_id']==ms[0]['id']==verified[0]['target_id']


# The two checks below own validate_proposals, not the wire. They build decisions
# directly so a selector change cannot quietly stop exercising the validator.
async def test_invalid_identity_reference_rejects_only_that_complete_proposal():
    source='甲见到乙。';ms=source_mentions('book',1,source,Names(names=[
        dict(surface='甲',kind='character',quote=source,named=True),
        dict(surface='乙',kind='character',quote=source,named=True)]),ONTOLOGY)
    decisions=[Decision(mention_id=ms[0]['id'],outcome='new',target_id='not-offered',
                        quote=source,evidence_start=0,reason='supported'),
               Decision(mention_id=ms[1]['id'],outcome='new',target_id=ms[1]['id'],
                        quote=source,evidence_start=0,reason='supported')]
    yes,no=validate_proposals(source,ms,{m['id']:[] for m in ms},
        Proposals(decisions=decisions,claims=[]),ONTOLOGY)
    assert len(yes)==1 and yes[0]['mention_id']==ms[1]['id']
    assert len(no)==1 and no[0]['rejection']=='new identity must reference an offered mention of the same kind'


def test_identity_rejects_incompatible_existing_candidate_kind():
    source='甲来了。';ms=source_mentions('book',1,source,Names(names=[
        dict(surface='甲',kind='character',quote=source,named=True)]),ONTOLOGY)
    # Deliberately malformed retrieval input proves application validation remains
    # authoritative even if a schema-constrained model selects the offered ref.
    candidates={ms[0]['id']:[dict(id='place-id',kind='place',canonical='甲地')]}
    decisions=[Decision(mention_id=ms[0]['id'],outcome='existing',target_id='place-id',
                        quote=source,evidence_start=0,reason='same')]
    yes,no=validate_proposals(source,ms,candidates,Proposals(decisions=decisions,claims=[]),ONTOLOGY)
    assert not yes and no[0]['rejection']=='invented candidate ID or incompatible kind'


def test_oversized_explanations_fail_model_validation():
    with pytest.raises(ValueError):
        IdentityDecisions(decisions=[dict(occurrence_ref='o1',outcome='unresolved',target_ref=None,
            reason_code='ambiguous',explanation='x'*201,quote='甲',evidence_start=0)])
    with pytest.raises(ValueError):
        ExtractProposals.model_validate(dict(names=[],attributes=[dict(
            subject_ref=dict(name_index=0,passage_id='p0',occurrence_index=0),
            attribute='description',value='x'*401,value_en='',passage_ids=['p0'])],
            relations=[],occurrences=[]))


def test_extract_rejects_an_occurrence_anchor_from_an_unoffered_passage():
    source='凌峰打开地图。\n他指出西南方向。'
    contract=PassageContract(source,max_chars=400,overlap=0)
    first,second=contract.passages
    body=dict(names=[dict(surface='凌峰',kind='character',passage_id=first['id'])],
              attributes=[dict(subject_ref=dict(name_index=0,passage_id=second['id'],
                  occurrence_index=0),attribute='description',value='打开地图',value_en='',
                  passage_ids=[second['id']])],relations=[],occurrences=[])
    with pytest.raises(ValueError,match='occurrence ordinal is out of range'):
        contract.materialize('extract',ExtractProposals,body,ONTOLOGY)


def test_extract_drops_non_adjacent_evidence_without_losing_other_items():
    source='凌峰打开地图。\n中间没有相关证据。\n她离开房间。'
    contract=PassageContract(source,max_chars=400,overlap=0)
    first,_,last=contract.passages
    body=dict(names=[dict(surface='凌峰',kind='character',passage_id=first['id'])],
              attributes=[dict(subject_ref=dict(name_index=0,passage_id=first['id'],
                  occurrence_index=0),attribute='description',value='打开地图',value_en='',
                  passage_ids=[first['id'],last['id']])],relations=[],occurrences=[])
    result=contract.materialize('extract',ExtractProposals,body,ONTOLOGY)
    assert result['attributes']==[]
    assert result['rejected'][0]['rejection']=='extract citations must be adjacent'


def test_extract_accepts_adjacent_cross_paragraph_evidence_and_keeps_union_span():
    source='凌峰打开地图。\n他指出西南方向。\n她离开房间。'
    contract=PassageContract(source,max_chars=400,overlap=0)
    first,second,unrelated=contract.passages
    body=dict(names=[dict(surface='凌峰',kind='character',passage_id=first['id'])],
              attributes=[dict(subject_ref=dict(name_index=0,passage_id=first['id'],
                  occurrence_index=0),attribute='description',value='指出西南方向',value_en='',
                  passage_ids=[first['id'],second['id']])],relations=[],occurrences=[])
    result=contract.materialize('extract',ExtractProposals,body,ONTOLOGY)
    assert len(result['attributes'])==1 and not result['rejected']
    evidence=KnowledgeEngine._evidence_for_citation(source,[first['id'],second['id']])
    assert evidence['quote']==source[:second['char_end']]
    assert unrelated['text'] not in evidence['quote']


def test_extract_rejects_empty_duplicate_and_unoffered_citation_lists():
    source='凌峰打开地图。';contract=PassageContract(source,max_chars=400,overlap=0)
    pid=contract.passages[0]['id']
    base=dict(subject_ref=dict(name_index=0,passage_id=pid,occurrence_index=0),
              attribute='description',value='打开地图',value_en='')
    body=dict(names=[dict(surface='凌峰',kind='character',passage_id=pid)],relations=[],occurrences=[])
    for refs in ([],[pid,pid],[pid,'missing','also-missing']):
        candidate=dict(body,attributes=[dict(base,passage_ids=refs)])
        with pytest.raises(ValueError,match='extract items must cite one or two distinct passages|extract citation is not offered'):
            contract.materialize('extract',ExtractProposals,candidate,ONTOLOGY)


def test_extract_schema_bounds_each_request_while_model_accepts_aggregate_name_inventory():
    source=''.join(f'名{i}。' for i in range(64))
    contract=PassageContract(source,max_chars=400,overlap=0)
    pid=contract.passages[0]['id']
    names=[dict(surface=f'名{i}',kind='character',passage_id=pid) for i in range(64)]
    model=ExtractProposals.model_validate(dict(names=names,attributes=[],relations=[],occurrences=[]))
    assert len(model.names)==64
    assert contract.schema('extract',ExtractProposals,ONTOLOGY)['properties']['names']['maxItems']==14


async def test_saturated_alignment_windows_subdivide_and_smallest_window_records_failure():
    """The regression this exists for: an unbounded alignments list let a recurring name
    force the model to emit one object per occurrence with no ceiling, exhausting
    num_predict and truncating the whole chapter's align response into unparseable
    partial JSON. Saturation must subdivide the window like claim extraction does,
    rather than let the chapter fail over a call that was never actually stuck."""
    from unittest.mock import AsyncMock
    from pipeline.knowledge import ALIGNMENT_LIMIT, MIN_ALIGNMENT_CHARS
    source='凌峰做了一件事。'*40
    display='Ling Feng did a thing. '*40
    mentions=[dict(id=f'm{i}',surface='凌峰',kind='character',
                   char_start=i*8,char_end=i*8+2,quote='凌峰做了一件事。') for i in range(40)]
    engine=object.__new__(KnowledgeEngine)
    engine.alignment_limit=ALIGNMENT_LIMIT
    engine.call=AsyncMock(return_value=Alignments(alignments=[
        dict(phrase='Ling Feng',occurrence=0,mention_id=None,quote='') for _ in range(ALIGNMENT_LIMIT)]))
    spans=await engine._align_window('book',1,source,display,mentions,0,len(display),'digest')
    assert engine.call.await_count>1, 'a saturated window must subdivide rather than accept a partial list'
    assert engine._alignment_subdivisions>0
    # Recursion terminated (this line runs at all) once windows shrank to the floor,
    # proving MIN_ALIGNMENT_CHARS actually bounds it rather than looping forever.
    windows=[len(c.args[2]['translation']) for c in engine.call.await_args_list]
    assert windows[-1]<=MIN_ALIGNMENT_CHARS*2
    assert isinstance(spans,list)


def test_runtime_diagnostics_record_input_size_policy_and_request_counts():
    engine=object.__new__(KnowledgeEngine)
    engine.extract_window_chars=1800
    engine.extract_limits={'names':14,'attributes':8,'relations':6,'occurrences':5}
    engine.identity_batch_size=24
    engine.identity_soft_bytes=24*1024
    engine.identity_hard_bytes=32*1024
    engine.align_window_chars=6000
    engine.alignment_limit=32
    engine._stage_requests={'extract':3,'identity_slots':2}
    engine._stage_cache_hits={'extract':1}
    engine._extract_subdivisions=2
    engine._identity_batch_sizes=[12,8]
    engine.extract_context_tokens=8192
    engine.extract_output_tokens=1024
    metrics=engine._runtime_diagnostics('甲乙','Alpha')
    assert metrics['input_size']==dict(source_chars=2,source_bytes=6,
                                       display_chars=5,display_bytes=5)
    assert metrics['chunk_policy']['extract_window_chars']==1800
    assert metrics['chunk_policy']['identity_occurrences_per_batch']==24
    assert metrics['extract_chunking']['subdivisions']==2
    assert metrics['stage_requests']['extract']==3
    assert metrics['stage_cache_hits']=={'extract':1}
    assert metrics['stage_fresh_calls']=={'extract':2,'identity_slots':2}


def test_long_chapters_are_split_below_the_prompt_target_budget():
    source='\n'.join(('段落'+str(i)+'。')*100 for i in range(200))
    passages=PassageContract(source).passages
    batches=[]; current=[]; size=0
    for row in passages:
        row_size=len(json.dumps(dict(id=row['id'],text=row['text']),ensure_ascii=False).encode())+2
        if current and size+row_size>8192:
            batches.append(current); current=[]; size=0
        current.append(row['id']); size+=row_size
    if current: batches.append(current)
    passages={p['id']:p for p in PassageContract(source).passages}
    assert all(len(batch)<=64 for batch in batches)
    assert all(sum(len(json.dumps(dict(id=pid,text=passages[pid]['text']),ensure_ascii=False).encode())+2
                   for pid in batch)<=8192 for batch in batches)
    assert len(source.encode())>42000 and len(batches)>1
    assert all(sum(len(json.dumps(dict(id=pid,text=passages[pid]['text']),ensure_ascii=False).encode())+2 for pid in batch)<=8192
               for batch in batches)


def test_short_paragraphs_are_packed_by_content_budget_not_line_count():
    source='\n'.join('安若素描述了星莲的位置。' for _ in range(20))
    passages=PassageContract(source).passages
    assert len(passages)==20
    assert all(len(row['text'])<=400 for row in passages)
    assert ''.join(row['text'] for row in passages).replace('\n','')==source.replace('\n','')


def test_passage_references_still_require_identity_and_claim_verification():
    source='凌峰看向姜梦月。\n梦魇神殿在远方。'
    c=PassageContract(source)
    ms=source_mentions('book',1,source,Names(names=[dict(surface='凌峰',kind='character',quote=c.passages[0]['text'],named=True)]),ONTOLOGY)
    mid=ms[0]['id'];first,second=c.passages
    body=dict(decisions=[dict(mention_id=mid,outcome='new',target_id=mid,passage_id=second['id'],reason='wrong occurrence')],
              claims=[dict(type='fact',mention_ids=[mid],attribute='description',value='is king',passage_id=first['id'])])
    proposals=c.materialize('propose',Proposals,body,ONTOLOGY)
    yes,no=validate_proposals(source,ms,[],proposals,ONTOLOGY)
    assert len(no)==1 and no[0]['rejection']=='identity evidence does not include this occurrence'
    assert len(yes)==1  # a literal quote is not semantic support
    assert not approved(yes,Verification(verdicts=[dict(id=yes[0]['id'],supported=False,reason='not stated')]))[0]
    body['claims'][0]['passage_id']='unknown'
    assert not validate_proposals(source,ms,[],c.materialize('propose',Proposals,body,ONTOLOGY),ONTOLOGY)[0]
    body['claims'][0]['quote']='invented quote'
    with pytest.raises(ValueError,match='not generate evidence'):
        c.materialize('propose',Proposals,body,ONTOLOGY)


def test_passage_alignment_keeps_unlinked_cards_and_rejects_wrong_occurrence():
    source='凌峰来了。\n凌峰走了。';c=PassageContract(source)
    ms=source_mentions('book',1,source,Names(names=[dict(surface='凌峰',kind='character',quote='凌峰来了。',named=True)]),ONTOLOGY)
    alignment=c.materialize('align',Alignments,dict(alignments=[
        dict(phrase='Ling Feng',occurrence=0,mention_id=ms[0]['id'],passage_id=c.passages[1]['id']),
        dict(phrase='Ling Feng',occurrence=1,mention_id=ms[1]['id'],passage_id=c.passages[1]['id'])]),ONTOLOGY)
    spans=aligned_mentions('book',1,source,'Ling Feng came. Ling Feng left.',ms,alignment)
    assert spans[0]['mention_id'] is None and spans[1]['mention_id']==ms[1]['id']


def test_new_prompt_cannot_resume_old_revision():
    from pipeline.config import Config
    with pytest.raises(ValueError,match='prompt changed'):
        KnowledgeEngine(None,Config.load(),dict(prompt_version='evidence-v2'))


@pytest.mark.db
async def test_names_probe_cannot_publish_or_qualify_graph(db_conn,monkeypatch,tmp_path):
    from unittest.mock import AsyncMock
    from pipeline.config import Config
    from pipeline import benchmark_knowledge as module
    source='凌峰来了。';identity=dict(provider='ollama',name='test')
    monkeypatch.setattr(module,'local_model',AsyncMock(return_value=identity))
    monkeypatch.setattr(module,'objects',lambda cfg:None)
    monkeypatch.setattr(module,'read_object',lambda *args:source)
    names=Names(names=[dict(surface='凌峰',kind='character',quote=source,evidence_start=0,named=True)],reviewed_kinds=ONTOLOGY['kinds'])
    async with db_conn.transaction(force_rollback=True):
        novel=await make_novel(db_conn,ontology=json.dumps(ONTOLOGY))
        ms=source_mentions(novel,1,source,names,ONTOLOGY)
        discover=AsyncMock(return_value=(ms,{'character':dict(proposed_surfaces=1,source_occurrences=1),
                       'place':dict(proposed_surfaces=0,source_occurrences=0),
                       'group':dict(proposed_surfaces=0,source_occurrences=0)},[]))
        monkeypatch.setattr(KnowledgeEngine,'discover_names',discover)
        snapshot=dict(chapters=[dict(chapter=1,raw_uri='saved',source_hash=digest(source))])
        cursor=await db_conn.execute('INSERT INTO graph_revision(novel_id,ontology,snapshot) VALUES(%s,%s,%s) RETURNING id',
            (novel,Jsonb(ONTOLOGY),Jsonb(snapshot)))
        base=str((await cursor.fetchone())[0])
        dataset=dict(chapters=[dict(chapter=1,source=source)],mentions=[dict(id=ms[0]['id'],chapter=1,surface='凌峰',kind='character',unambiguous=True)])
        path=tmp_path/'probe.json'
        await module.probe_names(db_conn,Config.load(),base,dataset,'test',path,1)
        result=json.loads(path.read_text())
        assert result['discovery_recall']==1
        assert 'activation_eligible' not in result
        assert result['status']=='completed'
        rid=result['revision']
        assert (await(await db_conn.execute('SELECT count(*) FROM entity WHERE revision_id=%s',(rid,))).fetchone())[0]==0
        assert (await(await db_conn.execute('SELECT count(*) FROM graph_job WHERE revision_id=%s',(rid,))).fetchone())[0]==0
        assert (await(await db_conn.execute('SELECT state,trusted,prompt_version FROM graph_revision WHERE id=%s',(rid,))).fetchone())==('staging',False,PROMPT_VERSION)


@pytest.mark.db
async def test_candidate_retrieval_is_kind_filtered_exact_first_and_capped_at_eight(db_conn):
    from pipeline.config import Config
    from tests.fixtures import FakeProvider,delete_novel
    novel=await make_novel(db_conn,ontology=json.dumps(ONTOLOGY))
    engine=None;original=None
    try:
        rid=str((await(await db_conn.execute('SELECT active_graph_revision FROM novel WHERE id=%s',(novel,))).fetchone())[0])
        vector='['+','.join(['0.01']*768)+']'
        await db_conn.execute('''INSERT INTO entity(id,novel_id,kind,canonical,first_seen_chapter,embedding)
            SELECT gen_random_uuid(),%s,'character','Historical '||n,1,%s::vector FROM generate_series(1,1000)n''',(novel,vector))
        exact=str((await(await db_conn.execute("""INSERT INTO entity(id,novel_id,kind,canonical,first_seen_chapter,embedding)
            VALUES(gen_random_uuid(),%s,'character','Ling Feng',1,%s::vector) RETURNING id""",(novel,vector))).fetchone())[0])
        await db_conn.execute("""INSERT INTO alias(entity_id,surface,lang,first_seen_chapter)
            VALUES(%s,'凌峰','zh',1)""",(exact,))
        await db_conn.execute("""INSERT INTO entity(id,novel_id,kind,canonical,first_seen_chapter,embedding)
            VALUES(gen_random_uuid(),%s,'place','Ling Feng',1,%s::vector)""",(novel,vector))
        revision=dict(id=rid,ontology=ONTOLOGY,model=dict(provider='ollama',name='test'))
        engine=KnowledgeEngine(db_conn,Config.load(),revision)
        original=engine.embedder;engine.embedder=FakeProvider(embed_dim=768)
        mention=dict(id='m1',surface='凌峰',kind='character',quote='凌峰 arrived.')
        candidates,_=await engine.candidates_for(25,[mention])
        assert len(candidates['m1'])==8 and candidates['m1'][0]['id']==exact
        assert all(row['kind']=='character' for row in candidates['m1'])
    finally:
        if engine and original: engine.embedder=original
        if engine: await engine.close()
        await delete_novel(db_conn,novel)


@pytest.mark.db
async def test_hosted_candidate_retrieval_never_constructs_or_calls_ollama(db_conn):
    from unittest.mock import AsyncMock
    from pipeline.config import Config
    from pipeline.inference_runtime import runtime_identity, effective_schema_transport
    from tests.fixtures import FakeProvider,delete_novel
    novel=await make_novel(db_conn,ontology=json.dumps(ONTOLOGY))
    engine=None
    try:
        rid=str((await(await db_conn.execute(
            'SELECT active_graph_revision FROM novel WHERE id=%s',(novel,))).fetchone())[0])
        exact=str((await(await db_conn.execute("""INSERT INTO entity
            (id,novel_id,kind,canonical,first_seen_chapter,revision_id)
            VALUES(gen_random_uuid(),%s,'character','Ling Feng',1,%s) RETURNING id""",
            (novel,rid))).fetchone())[0])
        await db_conn.execute("""INSERT INTO alias
            (entity_id,surface,lang,first_seen_chapter,revision_id)
            VALUES(%s,'凌峰','zh',1,%s)""",(exact,rid))
        await db_conn.execute("""INSERT INTO entity
            (id,novel_id,kind,canonical,first_seen_chapter,revision_id)
            VALUES(gen_random_uuid(),%s,'character','Jiang Mengyue',2,%s)""",(novel,rid))
        provider=FakeProvider(provider='groq')
        provider.aclose=AsyncMock()
        cfg=Config.load()
        revision=dict(id=rid,novel_id=novel,ontology=ONTOLOGY,
            model=dict(provider='groq',name='openai/gpt-oss-120b',strategy='api_two_pass',
                    identity=runtime_identity(output_tokens=cfg.hosted_graph_output_tokens,
                        schema_transport=effective_schema_transport(provider,'openai/gpt-oss-120b'),
                        context_tokens=cfg.hosted_graph_context_tokens,
                        request_tokens=cfg.hosted_graph_request_tokens)))
        engine=KnowledgeEngine(db_conn,cfg,revision,provider=provider)
        assert engine.embedder is None
        mention=dict(id='m1',surface='凌峰',kind='character',quote='凌峰 arrived.')
        candidates,vectors=await engine.candidates_for(25,[mention])
        assert vectors == {}
        assert candidates['m1'][0]['id']==exact
        assert all(row['kind']=='character' for row in candidates['m1'])
        assert provider.calls == []
    finally:
        if engine: await engine.close()
        await delete_novel(db_conn,novel)


def mentions(source='凌峰看向姜梦月。凌峰走进梦魇神殿。'):
    return source_mentions('book',1,source,Names(names=[dict(surface=name,kind=kind,quote=source,named=True)
        for name,kind in [('凌峰','character'),('姜梦月','character'),('梦魇神殿','place')]]),ONTOLOGY)


def test_occurrences_are_stable_distinct_and_source_validated():
    ms=mentions()
    assert ms==mentions()
    assert len({m['id'] for m in ms if m['surface']=='凌峰'})==2
    assert source_mentions('book',1,'凌峰。',Names(names=[dict(surface='Fengling',kind='character',quote='凌峰。',named=True)]),ONTOLOGY)==[]
    assert source_mentions('book',1,'长老。',Names(names=[dict(surface='长老',kind='character',quote='长老。',named=True)]),ONTOLOGY)==[]
    assert passage('😀凌峰。','凌峰。')['char_start']==1
    # Coverage output must not make the last proposed kind win for a shared surface.
    conflicting=Names(names=[dict(surface='凌峰',kind=k,quote='凌峰来了。',named=True)
                            for k in ['character','place','character']])
    assert source_mentions('book',1,'凌峰来了。',conflicting,ONTOLOGY)==[]


def test_invented_ids_invalid_kinds_and_unoffered_claims_are_rejected():
    source='凌峰看向姜梦月。凌峰走进梦魇神殿。';ms=mentions(source)
    decisions=[dict(mention_id=m['id'],outcome='existing',target_id='fengling',quote=m['quote'],reason='same') for m in ms]
    candidates=[dict(id='fengling',kind='character')]
    proposals=Proposals(decisions=decisions,claims=[dict(type='fact',mention_ids=['invented'],attribute='description',value='king',quote=source)])
    yes,no=validate_proposals(source,ms,candidates,proposals,ONTOLOGY)
    assert len(no)==2  # Place -> character, plus invented claim subject.
    # Structural validity isn't identity evidence. Independent verifier rejects the merge.
    approved_items,rejected=approved(yes,Verification(verdicts=[dict(id=x['id'],supported=False,reason='not the same person') for x in yes]))
    assert not approved_items and len(rejected)==len(yes)
    assert approved(yes,Verification(verdicts=[]))[0]==[]


def test_alignment_keeps_unlinked_phrases_and_disambiguates_occurrences():
    source='凌峰看向姜梦月。凌峰走进梦魇神殿。';ms=mentions(source)
    ling,jiang=ms[0],ms[1]
    spans=aligned_mentions('book',1,source,'Feng spoke to Feng. Unknown smiled.',ms,Alignments(alignments=[
        dict(phrase='Feng',occurrence=0,mention_id=ling['id'],quote=ling['quote']),
        dict(phrase='Feng',occurrence=1,mention_id=jiang['id'],quote=jiang['quote']),
        dict(phrase='Unknown',occurrence=0,mention_id='invented',quote='fake'),
    ]))
    assert [s['mention_id'] for s in spans]==[ling['id'],jiang['id'],None]
    assert spans[0]['id']!=spans[1]['id']


async def test_staging_rebuild_never_touches_global_glossary():
    from unittest.mock import AsyncMock
    db=AsyncMock()
    await promote_verified_glossary(db,dict(id='r',novel_id='n',state='staging',trusted=False,legacy=False),1,'en')
    db.execute.assert_not_awaited()


@pytest.mark.db
async def test_terminal_graph_failure_blocks_later_chapters_chronologically(db_conn):
    from tests.fixtures import delete_novel
    novel=await make_novel(db_conn,ontology=json.dumps(ONTOLOGY))
    try:
        old=(await(await db_conn.execute('SELECT active_graph_revision FROM novel WHERE id=%s',(novel,))).fetchone())[0]
        await db_conn.execute("UPDATE graph_revision SET state='archived' WHERE id=%s",(old,))
        rid=(await(await db_conn.execute("""INSERT INTO graph_revision(novel_id,state,trusted,ontology)
            VALUES(%s,'active',true,%s) RETURNING id""",(novel,Jsonb(ONTOLOGY)))).fetchone())[0]
        await db_conn.execute('UPDATE novel SET active_graph_revision=%s WHERE id=%s',(rid,novel))
        await db_conn.execute("""INSERT INTO graph_job(revision_id,chapter_index,state,input_hash,model_identity,generation,attempts)
            VALUES(%s,1,'failed','a','m',1,4),(%s,2,'pending','b','m',1,0)""",(rid,rid))
        assert await next_retryable_active_revision(db_conn) is None
        await db_conn.execute("UPDATE graph_job SET attempts=3,retry_at=now()-interval '1 second' WHERE revision_id=%s AND chapter_index=1",(rid,))
        assert await next_retryable_active_revision(db_conn)==str(rid)
        assert await next_retryable_active_revision(db_conn, novel_id=str(uuid4())) is None
        assert await next_retryable_active_revision(db_conn, novel_id=novel)==str(rid)
    finally:
        await delete_novel(db_conn,novel)


def test_graph_retry_schedule_is_bounded_then_terminal():
    assert [graph_retry_delay_minutes(attempt) for attempt in range(1,6)]==[5,15,45,None,None]


@pytest.mark.db
async def test_revision_fence_evidence_retry_and_later_binding(db_conn):
    db=db_conn
    async with db.transaction(force_rollback=True):
        novel=await make_novel(db,ontology=json.dumps(ONTOLOGY))
        source='凌峰看向姜梦月。凌峰走进梦魇神殿。'
        rid=str(uuid4())
        await db.execute('''INSERT INTO graph_revision(id,novel_id,ontology,model,snapshot)
            VALUES(%s,%s,%s,%s,%s)''',(rid,novel,Jsonb(ONTOLOGY),Jsonb(dict(provider='ollama',name='test')),
            Jsonb(dict(chapters=[dict(chapter=1,source_hash=digest(source)),dict(chapter=10,source_hash=digest('十章揭晓。'))]))))
        ms=source_mentions(novel,1,source,Names(names=[dict(surface=n,kind=k,quote=source,named=True)
            for n,k in [('凌峰','character'),('姜梦月','character'),('梦魇神殿','place')]]),ONTOLOGY)
        roots={}
        for m in ms: roots.setdefault(m['surface'],m['id'])
        output=dict(mentions=ms,spans=[],rejected=[],display_hash=digest(source),items=[
            dict(id=f'identity:{i}',mention_id=m['id'],outcome='new',target_id=roots[m['surface']],quote=m['quote'])
            for i,m in enumerate(ms) if m['surface']!='姜梦月'])
        output['items'].append(dict(id='claim:0',type='fact',mention_ids=[ms[0]['id']],attribute='description',value='Looks at Jiang Mengyue.',quote=ms[0]['quote']))
        engine=object.__new__(KnowledgeEngine);engine.db=db;engine.revision=dict(id=rid)
        state=PipelineState(envelope=ChapterEnvelope(novel_id=novel,chapter_index=1,source_lang='zh',raw_text=source))
        await db.execute("""INSERT INTO glossary(novel_id,source_term,target_term,locked_at_chapter,constraint_class)
            VALUES(%s,'凌峰','Ling Feng',0,'character_name')""",(novel,))
        await db.execute("SELECT set_config('app.graph_revision',%s,true),set_config('app.graph_generation','1',true)",(rid,))
        await engine.publish(state,output,source)
        await engine.publish(state,output,source)
        assert (await(await db.execute('SELECT count(*) FROM fact WHERE revision_id=%s',(rid,))).fetchone())[0]==1
        assert state.resolutions[ms[0]['id']]!=state.resolutions[ms[-1]['id']]
        assert (await(await db.execute('SELECT count(*) FROM entity WHERE revision_id=%s',(rid,))).fetchone())[0]==2
        # A chapter-one occurrence can acquire a supported identity at chapter ten.
        jiang=next(m for m in ms if m['surface']=='姜梦月')
        late_evidence=str(uuid4());late_entity=str(uuid4())
        await db.execute('INSERT INTO graph_evidence VALUES(%s,%s,%s,10,%s,0,5,%s)',(late_evidence,rid,novel,digest('十章揭晓。'),'十章揭晓。'))
        await db.execute('INSERT INTO entity(id,novel_id,kind,canonical,first_seen_chapter,revision_id) VALUES(%s,%s,%s,%s,10,%s)',(late_entity,novel,'character','姜梦月',rid))
        await db.execute('INSERT INTO mention_binding VALUES(%s,%s,10,%s,%s)',(rid,jiang['id'],late_entity,late_evidence))
        await db.execute("SELECT set_config('app.novel_id',%s,true),set_config('app.current_chapter','9',true)",(novel,))
        await db.execute('SET LOCAL ROLE rls_reader')
        assert (await(await db.execute('SELECT count(*) FROM entity WHERE revision_id=%s',(rid,))).fetchone())[0]==0 # staging
        await db.execute('RESET ROLE')
        await db.execute("UPDATE graph_revision SET state='archived',trusted=false WHERE novel_id=%s AND state='active'",(novel,))
        await db.execute("UPDATE graph_revision SET state='active',trusted=true WHERE id=%s",(rid,))
        await db.execute('UPDATE novel SET active_graph_revision=%s WHERE id=%s',(rid,novel))
        await db.execute('SET LOCAL ROLE rls_reader')
        assert (await(await db.execute('SELECT count(*) FROM mention_binding WHERE mention_id=%s',(jiang['id'],))).fetchone())[0]==0
        await db.execute("SELECT set_config('app.current_chapter','10',true)")
        assert (await(await db.execute('SELECT count(*) FROM mention_binding WHERE mention_id=%s',(jiang['id'],))).fetchone())[0]==1
        await db.execute('RESET ROLE')
        await db.execute('UPDATE graph_revision SET generation=generation+1 WHERE id=%s',(rid,))
        with pytest.raises(Exception,match='stale graph generation'):
            async with db.transaction():
                await db.execute('INSERT INTO entity(id,novel_id,kind,canonical,first_seen_chapter,revision_id) VALUES(%s,%s,%s,%s,1,%s)',(str(uuid4()),novel,'character','stale',rid))
        await db.execute("SELECT set_config('app.graph_revision','',true)")
        with pytest.raises(Exception,match='fenced'):
            async with db.transaction():
                await db.execute('INSERT INTO entity(id,novel_id,kind,canonical,first_seen_chapter) VALUES(%s,%s,%s,%s,1)',(str(uuid4()),novel,'character','legacy'))


@pytest.mark.db
async def test_glossary_votes_use_distinct_chapters(db_conn):
    from pipeline.stages.resolve import _record_candidate
    async with db_conn.transaction(force_rollback=True):
        novel=await make_novel(db_conn)
        assert await _record_candidate(db_conn,novel,'凌峰','Ling Feng',1)==1
        assert await _record_candidate(db_conn,novel,'凌峰','Ling Feng',2)==2
        assert await _record_candidate(db_conn,novel,'凌峰','Ling Feng',1)==2


@pytest.mark.db
async def test_rollback_never_retrusts_legacy_graph(db_conn):
    from pipeline.graph_rebuild import switch
    db=db_conn
    async with db.transaction(force_rollback=True):
        novel=await make_novel(db,ontology=json.dumps(ONTOLOGY))
        old=(await(await db.execute('SELECT active_graph_revision FROM novel WHERE id=%s',(novel,))).fetchone())[0]
        await db.execute("UPDATE graph_revision SET state='archived',trusted=false WHERE id=%s",(old,))
        new=str(uuid4())
        await db.execute("INSERT INTO graph_revision(id,novel_id,ontology,state,trusted) VALUES(%s,%s,%s,'active',true)",(new,novel,Jsonb(ONTOLOGY)))
        await db.execute('UPDATE novel SET active_graph_revision=%s WHERE id=%s',(new,novel))
        await switch(db,None,str(old))
        row=await(await db.execute('SELECT state,trusted,generation,version FROM graph_revision WHERE id=%s',(old,))).fetchone()
        assert row==('active',False,2,2)
        await db.execute("SELECT set_config('app.novel_id',%s,true),set_config('app.current_chapter','10',true)",(novel,))
        await db.execute('SET LOCAL ROLE rls_reader')
        assert (await(await db.execute('SELECT reader_graph_revision()')).fetchone())[0] is None
        status=await(await db.execute('SELECT status FROM reader_knowledge_status(10)')).fetchone()
        assert status==('repair',)
        await db.execute('RESET ROLE')


async def test_thinking_joins_identity_only_when_configured(monkeypatch):
    """Reasoning changes what the model produces, so a set value is pinned. Unset must be
    OMITTED, not defaulted, or every identity recorded before the flag existed stops
    comparing equal and its revision becomes unresumable."""
    from dataclasses import replace
    from pipeline.config import Config, graph_runtime
    monkeypatch.setenv('GRAPH_OLLAMA_FIRST_TOKEN_SECONDS','900')
    monkeypatch.setenv('GRAPH_OLLAMA_TIMEOUT_SECONDS','30')
    # Pinned too: this test asserts 1800 below, and inheriting a real deployment's tuned
    # deadline from .env made it fail the moment that value was raised.
    monkeypatch.setenv('GRAPH_OLLAMA_TOTAL_TIMEOUT_SECONDS','1800')
    monkeypatch.delenv('GRAPH_OLLAMA_THINK', raising=False)
    cfg = Config.load()
    assert cfg.graph_ollama_think is None
    assert 'think' not in graph_runtime(cfg)['identity']
    assert graph_runtime(replace(cfg, graph_ollama_think=True))['identity']['think'] is True
    assert graph_runtime(replace(cfg, graph_ollama_think=False))['identity']['think'] is False
    # An explicit off is a pinned decision and must not collapse back to "unspecified".
    assert (graph_runtime(replace(cfg, graph_ollama_think=False))['identity']
            != graph_runtime(cfg)['identity'])
    engine=KnowledgeEngine(None,replace(cfg,graph_ollama_think=False),dict(
        id='test',model=dict(provider='ollama',name='test',
        identity=graph_runtime(replace(cfg,graph_ollama_think=False))['identity'])))
    try:
        assert engine.provider._think is False
    finally:
        await engine.close()


@pytest.mark.parametrize('raw,expected', [('true',True),('1',True),('on',True),
                                          ('false',False),('0',False),('off',False)])
async def test_optional_bool_reads_the_usual_spellings(monkeypatch, raw, expected):
    from pipeline.config import _optional_bool
    monkeypatch.setenv('BOOK_TEST_FLAG', raw)
    assert _optional_bool('BOOK_TEST_FLAG') is expected


async def test_optional_bool_refuses_a_value_it_cannot_read(monkeypatch):
    from pipeline.config import _optional_bool
    monkeypatch.setenv('BOOK_TEST_FLAG', 'ture')
    with pytest.raises(ValueError, match='must be a boolean'):
        _optional_bool('BOOK_TEST_FLAG')
