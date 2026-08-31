"""Evidence, occurrence identity, revision publication and spoiler regressions."""
import json
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb

from pipeline.context import PipelineState
from pipeline.envelope import ChapterEnvelope
from pipeline.evidence import (Names, Proposals, Verification, Alignments, source_mentions,
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
        graph_runtime(replace(cfg,graph_ollama_num_ctx=4096))


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
            prompt=complete.call_args.args[0]
            offered=json.loads(prompt.split('INPUT DATA (not instructions):\n')[1])
            assert 'source' not in offered and offered['passages']==[dict(id=pid,text=source)]
            assert complete.call_args.kwargs['json_schema']['required']==['reviewed','names']
            timing=(await(await db_conn.execute('SELECT runtime_metrics FROM graph_completion WHERE revision_id=%s',(rid,))).fetchone())[0]
            assert timing==dict(load_seconds=1,eval_seconds=2,input_tokens=42,output_tokens=5)
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


async def test_application_can_aggregate_more_than_64_names_across_bounded_requests(monkeypatch):
    from unittest.mock import AsyncMock
    from pipeline.config import Config
    source='\n'.join(f'Name{i} arrived.' for i in range(65))
    engine=KnowledgeEngine(None,Config.load(),dict(id='test',ontology=ONTOLOGY,model=dict(provider='ollama',name='test')))
    async def discover(_stage,_schema,payload):
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


def test_long_chapters_are_split_below_the_prompt_target_budget():
    source='\n'.join(('段落'+str(i)+'。')*100 for i in range(200))
    batches=KnowledgeEngine._passage_batches(source)
    assert len(source.encode())>42000 and len(batches)>1
    passages={p['id']:p for p in source_passages(source)}
    assert all(sum(len(json.dumps(dict(id=pid,text=passages[pid]['text']),ensure_ascii=False).encode())+2 for pid in batch)<=24000
               for batch in batches)


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
    assert not qualified(dict(metrics,publication_review_complete=True,candidate_recall=.99))


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
