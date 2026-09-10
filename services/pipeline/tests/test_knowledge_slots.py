"""Exercise actual request schemas, materialization, verification and publication."""
import json
from unittest.mock import AsyncMock

import pytest
from psycopg.types.json import Jsonb

from pipeline.config import Config
from pipeline.context import PipelineState
from pipeline.envelope import ChapterEnvelope
from pipeline.evidence import Names, digest, source_mentions, PROMPT_VERSION
from pipeline.knowledge import IDENTITY_PROMPT_SOFT_BYTES, KnowledgeEngine
from pipeline.knowledge_contract import (
    compact_identity_payload, identity_schema, materialize_identity, materialize_verification,
    unique_json_object, verification_schema,
)
from pipeline.passages import PassageContract, source_passages
from pipeline.benchmark_knowledge import reviewed_fact_recall, summarize_runtime_calls
from novel_llm.provider import Completion
from tests.fixtures import make_novel

ONTOLOGY=dict(kinds=['character'],relations=[],attributes=[dict(name='description',kinds=['character'])])


def test_slots_enforce_per_occurrence_choices_and_duplicate_keys():
    contract=PassageContract('青山来了。')
    pid=contract.passages[0]['id']
    rows=[dict(occurrence_ref='o1',subject=dict(surface='青山',kind='character',
        quote='青山来了。',char_start=0,char_end=2),
        choices={'new:self':('new','o1','new_first_appearance'),
                 'unresolved':('unresolved',None,'ambiguous')})]
    schema=identity_schema(rows,[pid])
    assert schema['required']==['o1']
    assert schema['properties']['o1']['enum']==['new:self','unresolved']
    body={'o1':'new:self'}
    assert materialize_identity(body,rows,contract).decisions[0].target_ref=='o1'
    body['o1']='existing:e1'
    with pytest.raises(ValueError,match='unoffered'): materialize_identity(body,rows,contract)
    with pytest.raises(ValueError,match='one unique'): materialize_identity({},rows,contract)
    with pytest.raises(ValueError,match='duplicate JSON'):
        json.loads('{"o1":1,"o1":2}',object_pairs_hook=unique_json_object)


def test_compact_identity_payload_interns_repeated_context_and_uses_passage_refs():
    source='青山来了。\n青河看见青山。'
    contract=PassageContract(source)
    candidate=dict(kind='character',canonical='旧人',
                   source_context=[dict(chapter=0,quote='旧人守在山门。')])
    rows=[]
    for index,(surface,start,quote) in enumerate((
            ('青山',0,'青山来了。'),('青河',6,'青河看见青山。')),1):
        rows.append(dict(occurrence_ref=f'o{index}',
            subject=dict(surface=surface,kind='character',char_start=start,
                         char_end=start+2,quote=quote),
            choices={'new:self':('new',f'o{index}','new_first_appearance'),
                     'existing:t1':('existing','t1','existing_evidence')},
            choice_context={'existing:t1':candidate}))
    payload=compact_identity_payload(rows,contract)
    identity=payload['identity']
    assert set(identity['choice_subjects'])=={'existing:t1'}
    assert all('quote' not in identity['subjects'][row[0]]
               for row in identity['occurrences'].values())
    serialized=json.dumps(payload,ensure_ascii=False)
    assert serialized.count('旧人守在山门。')==1
    assert serialized.count('青山来了。')==1


def test_dense_identity_matrix_stays_below_compact_prompt_target():
    lines=[f'人物{i}在长廊中看见了其他人。'+('甲乙丙丁'*18) for i in range(12)]
    source='\n'.join(lines);mentions=[];offset=0
    for i,line in enumerate(lines):
        surface=f'人物{i}'
        mentions.append(dict(surface=surface,kind='character',char_start=offset,
            char_end=offset+len(surface),quote=line))
        offset+=len(line)+1
    shared=[dict(kind='character',canonical=f'旧人{i}',
                 source_context=[dict(chapter=0,quote=f'旧人{i}曾在长廊中出现。')])
            for i in range(8)]
    rows=[]
    for i,subject in enumerate(mentions):
        choices={'new:self':('new',f'o{i+1}','new_first_appearance'),
                 'unresolved':('unresolved',None,'ambiguous')}
        contexts={}
        for j,other in enumerate(mentions):
            if i==j: continue
            token=f'new:o{j+1}';choices[token]=('new',f'o{j+1}','new_coreference')
            contexts[token]=other
        for j,candidate in enumerate(shared):
            token=f'existing:t{j+1}';choices[token]=('existing',f't{j+1}','existing_evidence')
            contexts[token]=candidate
        rows.append(dict(occurrence_ref=f'o{i+1}',subject=subject,
                         choices=choices,choice_context=contexts))
    request=dict(source=source,identity_occurrences=rows,
                 _passage_ids=[p['id'] for p in PassageContract(source).passages])
    engine=_engine()
    metrics=engine._identity_request_size(source,request)
    old_matrix_bytes=len(json.dumps(rows,ensure_ascii=False).encode())
    assert old_matrix_bytes>30_000
    assert metrics['prompt_bytes']<12*1024
    assert metrics['request_material_bytes']<IDENTITY_PROMPT_SOFT_BYTES


def test_verification_slots_are_inline_exact_and_bounded():
    schema=verification_schema(['v1','v2'])
    assert '$defs' not in schema
    assert schema['required']==['v1','v2']
    assert schema['properties']['v1']['properties']['reason']['maxLength']==200
    body={'v1':dict(supported=True,reason='explicit'),
          'v2':dict(supported=False,reason='unsupported')}
    result=materialize_verification(body,['v1','v2'])
    assert [(v.id,v.supported) for v in result.verdicts]==[('v1',True),('v2',False)]
    with pytest.raises(ValueError,match='one unique verdict'):
        materialize_verification({'v1':body['v1']},['v1','v2'])
    with pytest.raises(ValueError,match='slot fields'):
        materialize_verification(dict(body,v1=dict(supported=True,reason='ok',extra=True)),['v1','v2'])
    with pytest.raises(ValueError,match='at most 200'):
        materialize_verification(dict(body,v1=dict(supported=True,reason='x'*201)),['v1','v2'])


def test_recall_requires_review_of_published_fact_ids():
    gold=[dict(id='g1'),dict(id='g2')];publications=[dict(id='f1',value='a different claim')]
    assert reviewed_fact_recall(gold,[])==0
    assert reviewed_fact_recall(gold,publications) is None
    assert reviewed_fact_recall(gold,publications,{'g1':['f1'],'g2':[]})==.5
    with pytest.raises(ValueError,match='unpublished'):
        reviewed_fact_recall(gold,publications,{'g1':['proposal-id'],'g2':[]})


def test_runtime_call_summary_keeps_stage_costs_and_retries_visible():
    summary=summarize_runtime_calls([
        ('extract',dict(input_tokens=100,output_tokens=20,request_seconds=1.25,stall_retries=1)),
        ('extract',dict(input_tokens=80,output_tokens=10,total_seconds=0.75)),
        ('verify',dict(input_tokens=40,output_tokens=5,request_seconds=0.5)),
    ])
    assert summary['extract']==dict(calls=2,input_tokens=180,output_tokens=30,
                                   inference_seconds=2.0,stall_retries=1)
    assert summary['verify']['calls']==1


@pytest.mark.db
async def test_current_extract_publication_writes_identity_and_source_fact(db_conn):
    """The merged extract output remains publishable through the current four-call contract."""
    source='青山打开地图。'
    names=Names(names=[dict(surface='青山',kind='character',named=True,
                           quote=source,evidence_start=0)])
    async with db_conn.transaction(force_rollback=True):
        novel=await make_novel(db_conn,ontology=json.dumps(ONTOLOGY))
        rid=str((await(await db_conn.execute("""INSERT INTO graph_revision(novel_id,ontology,prompt_version,snapshot)
            VALUES(%s,%s,%s,%s) RETURNING id""",(novel,Jsonb(ONTOLOGY),PROMPT_VERSION,
            Jsonb(dict(chapters=[dict(chapter=1,source_hash=digest(source))]))))).fetchone())[0])
        ms=source_mentions(novel,1,source,names,ONTOLOGY)
        await db_conn.execute("""INSERT INTO glossary(novel_id,source_term,target_term,locked_at_chapter,constraint_class)
            VALUES(%s,'青山','Qing Shan',0,'character_name')""",(novel,))
        engine=KnowledgeEngine(db_conn,Config.load(),dict(id=rid,novel_id=novel,ontology=ONTOLOGY,
            model=dict(provider='ollama',name='test')))
        mid=ms[0]['id']
        output=dict(mentions=ms,spans=[],rejected=[],display_hash=digest(''),items=[
            dict(id='identity:0',mention_id=mid,outcome='new',target_id=mid,
                 quote=source,evidence_start=0),
            dict(id='claim:0',type='fact',mention_ids=[mid],attribute='description',
                 value='打开地图',value_en='opened a map',quote=source,evidence_start=0)],
            diagnostics={})
        state=PipelineState(envelope=ChapterEnvelope(novel_id=novel,chapter_index=1,
            source_lang='zh',raw_text=source))
        try:
            await db_conn.execute("SELECT set_config('app.graph_revision',%s,true),set_config('app.graph_generation','1',true)",(rid,))
            await engine.publish(state,output,source)
            assert state.resolutions[mid]
            assert (await(await db_conn.execute(
                'SELECT canonical FROM entity WHERE revision_id=%s',(rid,))).fetchone())[0]=='Qing Shan'
            assert (await(await db_conn.execute(
                'SELECT value,value_en,source_chapter,valid_from_chapter FROM fact WHERE revision_id=%s',(rid,))).fetchone())==(
                    '打开地图','opened a map',1,1)
            assert output['diagnostics']['published_facts']==1
        finally:
            await engine.close()


async def test_oversized_identity_context_halves_batch_and_records_sizes():
    from pipeline.evidence import IdentityDecisions, Verification
    lines=[f'青山{i}来了。' for i in range(7)]
    source='\n'.join(lines);mentions=[];offset=0
    for i,line in enumerate(lines):
        surface=f'青山{i}'
        mentions.append(dict(id=f'm{i}',surface=surface,kind='character',
            char_start=offset,char_end=offset+len(surface),quote=line))
        offset+=len(line)+1
    engine=_engine()
    async def call(stage,_schema,payload):
        if stage=='identity_slots':
            if len(payload['identity_occurrences'])>3:
                # Match the production call() guard exactly. This spelling regressed in
                # production while the older test-only "hard local" spelling still passed.
                raise ValueError('graph context exceeds hard model budget; bounded caller contract regressed')
            return IdentityDecisions(decisions=[dict(occurrence_ref=row['occurrence_ref'],
                outcome='new',target_ref=row['occurrence_ref'],reason_code='new_first_appearance',
                explanation='first appearance',quote=row['subject']['quote'],
                evidence_start=row['subject']['char_start']) for row in payload['identity_occurrences']])
        return Verification(verdicts=[dict(id=item['item_ref'],supported=True,reason='supported')
                                      for item in payload['items']])
    engine.call=call
    verified,rejected,count=await engine._resolve_incremental(
        source,mentions,{m['id']:[] for m in mentions},ONTOLOGY)
    assert not rejected and count==len(verified)==7
    assert engine._identity_batch_sizes==[3,3,1]
    assert engine._identity_context_splits==1


async def test_identity_batches_are_packed_by_serialized_bytes_before_inference():
    from pipeline.evidence import IdentityDecisions, Verification
    lines=[f'青山{i}来了。' for i in range(6)]
    source='\n'.join(lines);mentions=[];offset=0
    for i,line in enumerate(lines):
        surface=f'青山{i}'
        mentions.append(dict(id=f'm{i}',surface=surface,kind='character',
            char_start=offset,char_end=offset+len(surface),quote=line))
        offset+=len(line)+1
    engine=_engine();engine.identity_soft_bytes=15_000;offered=[]
    engine._identity_request_size=lambda _source,payload: dict(
        request_material_bytes=(20_000 if len(payload['identity_occurrences'])>4 else 10_000))
    async def call(stage,_schema,payload):
        if stage=='identity_slots':
            offered.append(len(payload['identity_occurrences']))
            return IdentityDecisions(decisions=[dict(occurrence_ref=row['occurrence_ref'],
                outcome='new',target_ref=row['occurrence_ref'],reason_code='new_first_appearance',
                explanation='first appearance',quote=row['subject']['quote'],
                evidence_start=row['subject']['char_start']) for row in payload['identity_occurrences']])
        return Verification(verdicts=[dict(id=item['item_ref'],supported=True,reason='supported')
                                      for item in payload['items']])
    engine.call=call
    verified,rejected,count=await engine._resolve_incremental(
        source,mentions,{m['id']:[] for m in mentions},ONTOLOGY)
    assert not rejected and count==len(verified)==6
    assert offered==[4,2]
    assert engine._identity_batch_sizes==[4,2]
    assert engine._identity_context_splits==2


async def test_failed_identity_batch_is_rejected_and_later_batch_continues():
    from pipeline.evidence import IdentityDecisions, Verification
    lines=[f'青山{i}来了。' for i in range(13)]
    source='\n'.join(lines);mentions=[];offset=0
    for i,line in enumerate(lines):
        surface=f'青山{i}'
        mentions.append(dict(id=f'm{i}',surface=surface,kind='character',
            char_start=offset,char_end=offset+len(surface),quote=line))
        offset+=len(line)+1
    engine=_engine();engine.identity_batch_size=12
    async def call(stage,_schema,payload):
        if stage=='identity_slots':
            if payload['_batch_id']=='identity-1-12':
                raise TimeoutError('model stopped responding after its in-call retries')
            return IdentityDecisions(decisions=[dict(occurrence_ref=row['occurrence_ref'],
                outcome='new',target_ref=row['occurrence_ref'],reason_code='new_first_appearance',
                explanation='first appearance',quote=row['subject']['quote'],
                evidence_start=row['subject']['char_start']) for row in payload['identity_occurrences']])
        return Verification(verdicts=[dict(id=item['item_ref'],supported=True,reason='supported')
                                      for item in payload['items']])
    engine.call=call
    verified,rejected,count=await engine._resolve_incremental(
        source,mentions,{m['id']:[] for m in mentions},ONTOLOGY)
    assert [item['mention_id'] for item in verified]==['m12']
    skipped=[item for item in rejected if item.get('rejection')=='identity batch skipped after model call failure']
    assert len(skipped)==12
    assert {item['failure_category'] for item in skipped}=={'timeout'}
    assert count==1
    assert engine._identity_batch_sizes==[1]


async def test_optional_bookkeeping_savepoint_allows_following_statement():
    """A failed optional write must not poison the connection transaction."""
    import asyncio
    class Savepoint:
        def __init__(self, events): self.events=events
        async def __aenter__(self): self.events.append('enter'); return self
        async def __aexit__(self,typ,_value,_traceback):
            self.events.append('rollback' if typ else 'release')
            return False
    class DB:
        def __init__(self): self.events=[]
        def transaction(self): return Savepoint(self.events)
        async def execute(self,sql,params=None):
            self.events.append(sql)
            if sql=='bad': raise RuntimeError('optional write failed')
            return object()
    engine=object.__new__(KnowledgeEngine)
    engine.db=DB();engine._db_lock=asyncio.Lock()
    with pytest.raises(RuntimeError,match='optional write failed'):
        await engine._optional_exec('bad')
    await engine._optional_exec('good')
    assert engine.db.events==['enter','bad','rollback','enter','good','release']


def _engine(limit=1):
    """A bare engine carrying only the scheduling primitives `_fan_out` reads."""
    import asyncio
    engine=object.__new__(KnowledgeEngine)
    engine.max_concurrent_calls=limit
    engine._call_slots=asyncio.Semaphore(limit)
    engine._db_lock=asyncio.Lock()
    # Keep scheduling-only fixtures compatible with the current incremental identity
    # packer, whose limits are instance configuration rather than module globals.
    engine.identity_batch_size=24
    engine.identity_soft_bytes=24*1024
    engine.alignment_limit=32
    return engine


async def test_fan_out_preserves_order_and_overlaps_only_when_allowed():
    """Independent calls may overlap, but results still arrive in submission order."""
    import asyncio, functools
    for limit,expect_overlap in ((1,False),(4,True)):
        engine=_engine(limit);live=0;peak=0
        async def unit(value):
            nonlocal live,peak
            live+=1;peak=max(peak,live)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            live-=1
            return value
        results=await engine._fan_out([functools.partial(unit,i) for i in range(6)])
        assert results==[0,1,2,3,4,5]
        assert (peak>1) is expect_overlap
    # _fan_out itself does not throttle: it starts every unit and lets the semaphore
    # inside call() decide how many reach the provider at once.
    assert peak==6


async def test_call_semaphore_bounds_how_many_requests_reach_the_provider():
    """The budget has to bind at the provider, not at the fan-out: that is what keeps a
    local model's VRAM and a hosted provider's rate limit out of trouble."""
    import asyncio, functools
    engine=_engine(2);live=0;peak=0
    async def request(_i):
        nonlocal live,peak
        async with engine._call_slots:
            live+=1;peak=max(peak,live)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            live-=1
    await engine._fan_out([functools.partial(request,i) for i in range(6)])
    assert peak==2


async def test_fan_out_reraises_the_first_failure_after_siblings_settle():
    """Callers catch precise types (ValueError, TimeoutError); an ExceptionGroup would
    silently stop matching, and an abandoned sibling would keep writing after the raise."""
    import asyncio, functools
    engine=_engine(4);finished=[]
    async def ok(tag):
        await asyncio.sleep(0);finished.append(tag);return tag
    async def boom(tag):
        await asyncio.sleep(0);finished.append(tag)
        raise ValueError(f'{tag} failed')
    with pytest.raises(ValueError,match='second failed'):
        await engine._fan_out([functools.partial(ok,'first'),
                               functools.partial(boom,'second'),
                               functools.partial(boom,'third'),
                               functools.partial(ok,'fourth')])
    # Every sibling ran to completion before the raise: nothing is left in flight.
    assert sorted(finished)==['first','fourth','second','third']


def test_passage_split_is_memoized_and_hands_back_an_independent_list():
    from pipeline.passages import _split_passages
    source='青山来了。\n另一个青山离开。'
    _split_passages.cache_clear()
    first=source_passages(source)
    second=source_passages(source)
    assert first==second and _split_passages.cache_info().hits==1
    # A caller may append a synthetic context range to its own result (call() does)
    # without corrupting the memo every other caller shares.
    first.append(dict(id='synthetic'))
    assert len(source_passages(source))==len(second)


async def test_identity_packing_bisects_to_the_largest_fitting_batch():
    from pipeline.evidence import IdentityDecisions, Verification
    lines=[f'青山{i}来了。' for i in range(12)]
    source='\n'.join(lines);mentions=[];offset=0
    for i,line in enumerate(lines):
        surface=f'青山{i}'
        mentions.append(dict(id=f'm{i}',surface=surface,kind='character',
            char_start=offset,char_end=offset+len(surface),quote=line))
        offset+=len(line)+1
    engine=_engine();engine.identity_soft_bytes=15_000;offered=[];probes=[]
    def measure(_source,payload):
        size=len(payload['identity_occurrences'])
        probes.append(size)
        return dict(request_material_bytes=10_000 if size<=7 else 20_000)
    engine._identity_request_size=measure
    async def call(stage,_schema,payload):
        if stage=='identity_slots':
            offered.append(len(payload['identity_occurrences']))
            return IdentityDecisions(decisions=[dict(occurrence_ref=row['occurrence_ref'],
                outcome='new',target_ref=row['occurrence_ref'],reason_code='new_first_appearance',
                explanation='first appearance',quote=row['subject']['surface'],
                evidence_start=row['subject']['char_start']) for row in payload['identity_occurrences']])
        return Verification(verdicts=[dict(id=item['item_ref'],supported=True,reason='supported')
                                      for item in payload['items']])
    engine.call=call
    verified,rejected,count=await engine._resolve_incremental(
        source,mentions,{m['id']:[] for m in mentions},ONTOLOGY)
    assert not rejected and count==len(verified)==12
    # 7 is the largest size that fits, and the metric still reports dropped occurrences.
    assert offered==[7,5] and engine._identity_batch_sizes==[7,5]
    assert engine._identity_context_splits==12-7
    # A decrement-and-rebuild scan would have probed 12,11,10,9,8,7 for the first batch.
    assert len([p for p in probes if p>5])<6


async def test_identity_sizing_probes_never_mutate_the_candidate_map():
    """Packing is a measurement. Only the batch actually sent may widen `candidates`,
    or a probe at a size that was never sent would leak into a later batch."""
    from copy import deepcopy
    from pipeline.evidence import IdentityDecisions, Verification
    lines=[f'青山{i}来了。' for i in range(6)]
    source='\n'.join(lines);mentions=[];offset=0
    for i,line in enumerate(lines):
        surface=f'青山{i}'
        mentions.append(dict(id=f'm{i}',surface=surface,kind='character',
            char_start=offset,char_end=offset+len(surface),quote=line))
        offset+=len(line)+1
    candidates={m['id']:[] for m in mentions}
    candidates['m0']=[dict(id='e1',kind='character',canonical='旧青山',source_context=[])]
    baseline=deepcopy(candidates);seen=[]
    engine=_engine()
    def measure(_source,payload):
        # Snapshot at every probe: a pure prepare() leaves the map untouched here.
        seen.append(deepcopy(candidates))
        return dict(request_material_bytes=10_000
                    if len(payload['identity_occurrences'])<=2 else 20_000)
    engine._identity_request_size=measure
    async def call(stage,_schema,payload):
        if stage=='identity_slots':
            return IdentityDecisions(decisions=[dict(occurrence_ref=row['occurrence_ref'],
                outcome=('existing' if 'existing:t1' in row['choices'] else 'new'),
                target_ref=('t1' if 'existing:t1' in row['choices'] else row['occurrence_ref']),
                reason_code=('existing_evidence' if 'existing:t1' in row['choices']
                             else 'new_first_appearance'),
                explanation='supported',quote=row['subject']['surface'],
                evidence_start=row['subject']['char_start']) for row in payload['identity_occurrences']])
        return Verification(verdicts=[dict(id=item['item_ref'],supported=True,reason='supported')
                                      for item in payload['items']])
    engine.call=call
    await engine._resolve_incremental(source,mentions,candidates,ONTOLOGY)
    assert seen, 'the packer never measured a request'
    # No probe observed a wider map than the one its own sent batch had already produced.
    assert seen[0]==baseline
    for snapshot in seen:
        for mid,rows in snapshot.items():
            assert len(rows)==len({c['id'] for c in rows}), f'{mid} accumulated a duplicate'
