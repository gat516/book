"""Evidence, occurrence identity, revision publication and spoiler regressions."""
import json
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb

from pipeline.context import PipelineState
from pipeline.envelope import ChapterEnvelope
from pipeline.evidence import (Names, Proposals, IdentityDecisions, ClaimProposals, Decision,
    Verification, Alignments, source_mentions,
    validate_proposals, aligned_mentions, approved, digest, stable_id, passage, PROMPT_VERSION)
from pipeline.passages import PassageContract, source_passages
from pipeline.graph_rebuild import qualified,next_retryable_active_revision,graph_retry_delay_minutes
from pipeline.graph_rebuild import promote_verified_glossary
from pipeline.knowledge import KnowledgeEngine
from tests.fixtures import make_novel

ONTOLOGY=dict(kinds=['character','place','group'],relations=['member_of'],attributes=[dict(name='description',kinds=['character','place','group'])])


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
        body=dict(reviewed={kind:True for kind in ONTOLOGY['kinds']},names=[
            dict(surface='凌峰',kind='character',passage_id=pid),
            dict(surface='梦魇神殿',kind='place',passage_id=pid)])
        complete=AsyncMock(return_value=Completion(text=json.dumps(body),served_provider='ollama',served_model='test',
            input_tokens=42,output_tokens=5,timings=dict(load_seconds=1,eval_seconds=2)))
        engine.provider.complete=complete
        try:
            for _ in range(2):
                result=await engine.call('names',Names,dict(source=source))
                assert len(result.names)==2 and all(n.quote==source and n.evidence_start==0 for n in result.names)
            complete.assert_awaited_once()
            assert engine._stage_requests=={'names':2}
            assert engine._stage_cache_hits=={'names':1}
            prompt=complete.call_args.args[0]
            offered=json.loads(prompt.split('INPUT DATA (not instructions):\n')[1])
            assert 'source' not in offered and offered['passages']==[dict(id=pid,text=source)]
            assert complete.call_args.kwargs['json_schema']['required']==['reviewed','names']
            timing=(await(await db_conn.execute('SELECT runtime_metrics FROM graph_completion WHERE revision_id=%s',(rid,))).fetchone())[0]
            assert {k:timing[k] for k in ('load_seconds','eval_seconds','input_tokens','output_tokens')}==dict(
                load_seconds=1,eval_seconds=2,input_tokens=42,output_tokens=5)
            assert timing['stage']=='names' and timing['batch_id']
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
        body=dict(reviewed={kind:True for kind in ONTOLOGY['kinds']},names=[])
        ok=Completion(text=json.dumps(body),served_provider='ollama',served_model='test')
        engine.provider.complete=AsyncMock(side_effect=[TimeoutError('stalled'),TimeoutError('stalled'),ok])
        engine.provider.last_stream_diagnostics={}
        try:
            result=await engine.call('names',Names,dict(source=source))
            assert result.names==[]
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
                await engine.call('names',Names,dict(source=source))
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
    schema=c.schema('names',Names,ONTOLOGY)
    assert schema['properties']['reviewed']['required']==ONTOLOGY['kinds']
    assert 'quote' not in json.dumps(schema)
    body=dict(reviewed={kind:True for kind in ONTOLOGY['kinds']},names=[
        dict(surface='凌峰',kind='character',passage_id=first['id']),
        dict(surface='梦魇神殿',kind='place',passage_id=first['id']),
        dict(surface='啸牙冒险团',kind='group',passage_id=second['id'])])
    names=c.materialize('names',Names,body,ONTOLOGY)
    assert {m['kind'] for m in source_mentions('book',1,c.source,names,ONTOLOGY)}==set(ONTOLOGY['kinds'])
    assert names.names[0].quote==first['text']
    body['names']=[dict(surface='啸牙冒险团',kind='group',passage_id=first['id']),
                   dict(surface='虚构组织',kind='group',passage_id='invented')]
    names=c.materialize('names',Names,body,ONTOLOGY)
    assert len(names.rejected)==2 and all(n.kind!='group' for n in names.names)
    del body['reviewed']['place']
    with pytest.raises(ValueError,match='every ontology kind'):
        c.materialize('names',Names,body,ONTOLOGY)


def test_claim_wire_schema_is_bounded_and_never_carries_quotes():
    source='凌峰来了。\n他成为首领。';c=PassageContract(source)
    claims=c.schema('claims',ClaimProposals,ONTOLOGY)
    cdef=claims['$defs']['ClaimProposal']
    assert set(claims['properties'])=={'claims'}
    assert claims['properties']['claims']['maxItems']==12
    assert cdef['properties']['value']['maxLength']==400
    assert cdef['properties']['passage_ids']['maxItems']==3
    assert 'quote' not in json.dumps(claims)


def _homonym_engine(decide):
    """A KnowledgeEngine stub whose identity selector is `decide` and whose verifier passes."""
    from pipeline.evidence import IdentityDecisions as _ID, Verification as _V
    engine=object.__new__(KnowledgeEngine)
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


def test_oversized_explanations_and_fact_values_fail_model_validation():
    with pytest.raises(ValueError):
        IdentityDecisions(decisions=[dict(occurrence_ref='o1',outcome='unresolved',target_ref=None,
            reason_code='ambiguous',explanation='x'*201,quote='甲',evidence_start=0)])
    with pytest.raises(ValueError):
        ClaimProposals(claims=[dict(type='fact',occurrence_refs=['o1'],attribute='description',
            value='x'*401,quote='甲',evidence_start=0)])


def test_claim_materialization_supports_pronoun_continuation_and_cross_paragraph_evidence():
    source='凌峰打开地图。\n他指出西南方向。';c=PassageContract(source,max_chars=400,overlap=0)
    first,second=c.passages
    result=c.materialize('claims',ClaimProposals,dict(claims=[dict(type='fact',
        occurrence_refs=['o1'],attribute='description',value='指出西南方向',
        passage_ids=[first['id'],second['id']])]),ONTOLOGY)
    assert result.claims[0].quote==source and result.claims[0].evidence_start==0


async def test_saturated_claim_windows_subdivide_and_smallest_window_records_failure():
    from unittest.mock import AsyncMock
    source='甲做了一件事。'*80
    mention=dict(id='m1',surface='甲',kind='character',char_start=0,char_end=1,quote='甲做了一件事。')
    engine=object.__new__(KnowledgeEngine)
    def saturated(*_args,**_kwargs):
        payload=_args[2]
        passage_id=payload['focus_passage_ids'][0]
        return ClaimProposals(claims=[dict(type='fact',occurrence_refs=['o1'],attribute='description',
            value=f'事实{i}',quote='甲做了一件事。',evidence_start=0) for i in range(12)])
    engine.call=AsyncMock(side_effect=saturated)
    focus=source_passages(source,max_chars=400,overlap=0)[0]
    claims,rejected,count=await engine._claims_for_focus(
        source,[mention],ONTOLOGY,focus,passage_chars=400)
    assert not claims and count>=12
    assert any('smallest source window' in row['rejection'] for row in rejected)
    assert engine.call.await_count>1


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


async def test_application_can_aggregate_more_than_64_names_across_bounded_requests(monkeypatch):
    from unittest.mock import AsyncMock
    from pipeline.config import Config
    source='\n'.join(f'Name{i} arrived.' for i in range(65))
    engine=KnowledgeEngine(None,Config.load(),dict(id='test',ontology=ONTOLOGY,model=dict(provider='ollama',name='test')))
    async def discover(_stage,_schema,payload):
        if _stage=='name_verify':
            return Verification(verdicts=[dict(id=item['item_ref'],supported=True,reason='named')
                                          for item in payload['items']])
        contract=PassageContract(source,set(payload['_passage_ids']))
        rows=[]
        for p in contract.passages:
            surface=p['text'].split()[0]
            rows.append(dict(surface=surface,kind='character',quote=p['text'],evidence_start=p['char_start'],named=True))
        return Names(names=rows,reviewed_kinds=ONTOLOGY['kinds'])
    monkeypatch.setattr(engine,'call',AsyncMock(side_effect=discover))
    try:
        names,mentions,_=await engine.discover_names('book',1,source)
        assert len(names.names)==65 and len(mentions)==65
        assert engine.call.await_count>=2
    finally:
        await engine.close()


async def test_saturated_batched_name_inventory_retries_each_passage():
    from pipeline.knowledge_contract import NAME_SLOT_COUNT
    source='\n'.join(f'Name{i} arrived.' for i in range(4))
    engine=object.__new__(KnowledgeEngine)
    calls=[]
    async def discover(_stage,_schema,payload):
        if _stage=='name_verify':
            return Verification(verdicts=[dict(id=item['item_ref'],supported=True,reason='named')
                                          for item in payload['items']])
        calls.append(list(payload['_passage_ids']))
        if len(payload['_passage_ids'])>1:
            payload['_proposed_count']=NAME_SLOT_COUNT
            return Names(names=[],reviewed_kinds=ONTOLOGY['kinds'])
        p=PassageContract(source,set(payload['_passage_ids'])).passages[0]
        surface=p['text'].split()[0]
        return Names(names=[dict(surface=surface,kind='character',quote=p['text'],
            evidence_start=p['char_start'],named=True)],reviewed_kinds=ONTOLOGY['kinds'])
    engine.call=discover
    engine.revision=dict(ontology=ONTOLOGY)
    names,mentions,_=await engine.discover_names('book',1,source)
    assert [len(batch) for batch in calls]==[4,2,1,1,2,1,1]
    assert len(names.names)==len(mentions)==4
    assert engine._name_metrics==dict(passages=4,top_level_batches=1,
                                      saturation_splits=3,incomplete_windows=0)


async def test_name_eligibility_rejects_generic_fragments_before_occurrence_expansion():
    source='凌峰看见淡银色。'
    engine=object.__new__(KnowledgeEngine)
    engine.revision=dict(ontology=ONTOLOGY)
    async def call(stage,_schema,payload):
        if stage=='name_slots':
            return Names(names=[
                dict(surface='凌峰',kind='character',quote=source,evidence_start=0,named=True),
                dict(surface='淡银色',kind='place',quote=source,evidence_start=0,named=True),
            ],reviewed_kinds=ONTOLOGY['kinds'])
        return Verification(verdicts=[dict(id=item['item_ref'],
            supported=item['surface']=='凌峰',reason='proper name' if item['surface']=='凌峰' else 'color')
            for item in payload['items']])
    engine.call=call
    names,mentions,_=await engine.discover_names('book',1,source)
    assert [name.surface for name in names.names]==['凌峰']
    assert [mention['surface'] for mention in mentions]==['凌峰']
    assert any(row.get('surface')=='淡银色' for row in names.rejected)


def test_runtime_diagnostics_record_input_size_policy_and_request_counts():
    engine=object.__new__(KnowledgeEngine)
    engine._stage_requests={'name_slots':3,'claims':2}
    engine._stage_cache_hits={'name_slots':1}
    engine._name_metrics=dict(passages=12,top_level_batches=3,
                              saturation_splits=1,incomplete_windows=0)
    engine._claim_subdivisions=2
    metrics=engine._runtime_diagnostics('甲乙','Alpha')
    assert metrics['input_size']==dict(source_chars=2,source_bytes=6,
                                       display_chars=5,display_bytes=5)
    assert metrics['chunk_policy']['claim_focus_chars']==1200
    assert metrics['chunk_policy']['identity_occurrences_per_batch']==12
    assert metrics['name_chunking']['saturation_splits']==1
    assert metrics['claim_subdivisions']==2
    assert metrics['stage_requests']['claims']==2
    assert metrics['stage_cache_hits']=={'name_slots':1}
    assert metrics['stage_fresh_calls']=={'claims':2,'name_slots':2}


def test_long_chapters_are_split_below_the_prompt_target_budget():
    source='\n'.join(('段落'+str(i)+'。')*100 for i in range(200))
    batches=KnowledgeEngine._passage_batches(source)
    passages={p['id']:p for p in PassageContract(source).passages}
    assert all(len(batch)<=64 for batch in batches)
    assert all(sum(len(json.dumps(dict(id=pid,text=passages[pid]['text']),ensure_ascii=False).encode())+2
                   for pid in batch)<=8192 for batch in batches)
    assert len(source.encode())>42000 and len(batches)>1
    passages={p['id']:p for p in source_passages(source)}
    assert all(sum(len(json.dumps(dict(id=pid,text=passages[pid]['text']),ensure_ascii=False).encode())+2 for pid in batch)<=24000
               for batch in batches)


def test_short_paragraphs_are_packed_by_content_budget_not_line_count():
    source='\n'.join('安若素描述了星莲的位置。' for _ in range(20))
    name_batches=KnowledgeEngine._passage_batches(source)
    claim_batches=KnowledgeEngine._claim_focus_batches(source)
    assert len(name_batches)==1
    assert len(claim_batches)==1
    assert sum(len(row['text']) for row in claim_batches[0])<=1200
    assert [pid for batch in name_batches for pid in batch]==[
        row['id'] for row in PassageContract(source).passages]
    assert [row['id'] for batch in claim_batches for row in batch]==[
        row['id'] for row in source_passages(source,max_chars=1200,overlap=0)]


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
        discover=AsyncMock(return_value=(names,ms,{}))
        monkeypatch.setattr(KnowledgeEngine,'discover_names',discover)
        snapshot=dict(chapters=[dict(chapter=1,raw_uri='saved',source_hash=digest(source))])
        cursor=await db_conn.execute('INSERT INTO graph_revision(novel_id,ontology,snapshot) VALUES(%s,%s,%s) RETURNING id',
            (novel,Jsonb(ONTOLOGY),Jsonb(snapshot)))
        base=str((await cursor.fetchone())[0])
        dataset=dict(chapters=[dict(chapter=1,source=source)],mentions=[dict(id=ms[0]['id'],chapter=1,surface='凌峰',kind='character',unambiguous=True)])
        path=tmp_path/'probe.json'
        await module.probe_names(db_conn,Config.load(),base,dataset,'test',path,1)
        result=json.loads(path.read_text())
        assert result['discovery_recall']==1 and result['activation_eligible'] is False
        assert not qualified(result)
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


def test_activation_requires_review_of_publications_not_only_model_scores():
    metrics=dict(reviewed=True,reviewed_mentions=64,reviewed_facts=30,link_precision=1,
                 unambiguous_recall=1,fact_precision=1,merge_regressions=0,evidence_valid=True)
    assert not qualified(metrics)
    assert qualified(dict(metrics,publication_review_complete=True,candidate_recall=1))
    assert not qualified(dict(metrics,publication_review_complete=True,candidate_recall=1,link_precision=.979))
    # Candidate retrieval belongs to model selection; publication review judges every
    # row produced by this concrete revision.
    assert qualified(dict(metrics,publication_review_complete=True,candidate_recall=.99))


def test_small_complete_publication_review_can_qualify():
    metrics=dict(reviewed=True,publication_review_complete=True,
                 total_mentions=1,total_facts=1,reviewed_mentions=1,reviewed_facts=1,
                 link_precision=1,unambiguous_recall=1,fact_precision=1,
                 merge_regressions=0,evidence_valid=True)
    assert qualified(metrics)
    assert not qualified(dict(metrics,total_facts=0,reviewed_facts=0))
    assert not qualified(dict(metrics,total_mentions=2))


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
        await switch(db,None,str(old),rollback=True)
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


def test_claim_proposal_cannot_generate_unchecked_display_english():
    from pipeline.evidence import Claim
    with pytest.raises(ValueError,match='extra'):
        ClaimProposals(claims=[dict(type='fact',occurrence_refs=['o1'],attribute='description',
            value='甲',value_en='invented gloss',quote='甲',evidence_start=0)])
    assert Claim(type='fact',mention_ids=['m1'],attribute='description',
                 value='甲',quote='甲',evidence_start=0).value_en==''


async def test_fact_verification_reads_evidence_before_and_separately_from_each_claim():
    from unittest.mock import AsyncMock
    from pipeline.evidence import EvidenceReading, FactComponentVerification
    source='莲池在海湾深处。星源兽喷吐雾气。'
    mentions=[
        dict(id='pond',surface='莲池',kind='place',char_start=0,char_end=2,quote=source),
        dict(id='beast',surface='星源兽',kind='group',char_start=8,char_end=11,quote=source),
    ]
    items=[
        dict(id='claim:0',type='fact',mention_ids=['pond'],attribute='description',
             value='喷吐雾气',quote=source,evidence_start=0),
        dict(id='claim:1',type='fact',mention_ids=['beast'],attribute='description',
             value='喷吐雾气',quote=source,evidence_start=0),
    ]
    engine=object.__new__(KnowledgeEngine);seen=[]
    async def call(stage,_schema,payload):
        seen.append((stage,payload))
        if stage=='evidence':
            # This call must be claim-independent: the proposed pond error cannot prime it.
            assert 'items' not in payload
            return EvidenceReading(statements=[
                dict(subject='星源兽',assertion='喷吐雾气'),
                dict(subject='莲池',assertion='蕴藏强大力量')])
        assert payload['items'][0]['evidence_reading']==[
            dict(subject='星源兽',assertion='喷吐雾气',qualifiers=[])]
        subject=payload['items'][0]['subjects'][0]['surface']
        return FactComponentVerification(verdicts=[dict(id='v1',
            subject_supported=subject=='星源兽',assertion_supported=True,
            qualifiers_supported=True,evidence_sufficient=True,reason='source assigns action to beasts')])
    engine.call=AsyncMock(side_effect=call)
    verdicts=await engine._verify_facts(source,items,mentions)
    assert [v.supported for v in verdicts.verdicts]==[False,True]
    assert [stage for stage,_ in seen]==['evidence','fact_verify','fact_verify']


async def test_fact_verification_requires_every_component():
    from unittest.mock import AsyncMock
    from pipeline.evidence import EvidenceReading, FactComponentVerification
    source='据安若素观察，深处几株星莲呈暗金色。'
    mention=dict(id='lotus',surface='星莲',kind='group',char_start=10,char_end=12,quote=source)
    item=dict(id='claim:0',type='fact',mention_ids=['lotus'],attribute='description',
              value='所有星莲呈暗金色',quote=source,evidence_start=0)
    engine=object.__new__(KnowledgeEngine)
    async def call(stage,_schema,_payload):
        if stage=='evidence':
            return EvidenceReading(statements=[dict(subject='深处几株星莲',assertion='呈暗金色',
                qualifiers=['据安若素观察','深处几株'])])
        return FactComponentVerification(verdicts=[dict(id='v1',subject_supported=True,
            assertion_supported=True,qualifiers_supported=False,evidence_sufficient=True,
            reason='claim drops reporter and subset')])
    engine.call=AsyncMock(side_effect=call)
    verdict=(await engine._verify_facts(source,[item],[mention])).verdicts[0]
    assert not verdict.supported and verdict.reason.startswith('qualifiers_supported:')


async def test_unfaithful_english_is_not_attached_to_verified_source_fact():
    from unittest.mock import AsyncMock
    from pipeline.evidence import FactRenderings
    source='星源兽向石莲喷吐雾气。'
    mention=dict(id='beast',surface='星源兽',kind='group',char_start=0,char_end=3,quote=source)
    items=[dict(id='claim:0',type='fact',mention_ids=['beast'],attribute='description',
                value='向石莲喷吐雾气',quote=source,evidence_start=0)]
    engine=object.__new__(KnowledgeEngine)
    async def call(stage,_schema,_payload):
        if stage=='render':
            return FactRenderings(renderings=[dict(id='r1',value_en='the lotuses emit mist')])
        return Verification(verdicts=[dict(id='r1',supported=False,reason='reverses actor')])
    engine.call=AsyncMock(side_effect=call)
    rendered=await engine._render_facts(source,items,[mention])
    assert rendered[0]['value_en']=='' and rendered[0]['value']=='向石莲喷吐雾气'
