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
    compact_identity_payload, identity_schema, materialize_identity, materialize_names, materialize_verification,
    name_schema,
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
    engine=object.__new__(KnowledgeEngine)
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


def test_name_slots_are_fixed_and_only_materialize_source_valid_names():
    source='凌峰来到碎星滩。'
    contract=PassageContract(source)
    schema=name_schema(list(contract.by_id),['character','place'])
    assert schema['required']==[f'n{i}' for i in range(1,9)]
    assert '$defs' not in schema and 'maxItems' not in json.dumps(schema)
    body={f'n{i}':None for i in range(1,9)}
    body['n1']=dict(surface='凌峰',kind='character',passage_id=contract.passages[0]['id'])
    names=materialize_names(body,contract,dict(kinds=['character','place']))
    assert [name.surface for name in names.names]==['凌峰']


def test_recall_requires_review_of_published_fact_ids():
    gold=[dict(id='g1'),dict(id='g2')];publications=[dict(id='f1',value='a different claim')]
    assert reviewed_fact_recall(gold,[])==0
    assert reviewed_fact_recall(gold,publications) is None
    assert reviewed_fact_recall(gold,publications,{'g1':['f1'],'g2':[]})==.5
    with pytest.raises(ValueError,match='unpublished'):
        reviewed_fact_recall(gold,publications,{'g1':['proposal-id'],'g2':[]})


def test_runtime_call_summary_keeps_stage_costs_and_retries_visible():
    summary=summarize_runtime_calls([
        ('claims',dict(input_tokens=100,output_tokens=20,request_seconds=1.25,stall_retries=1)),
        ('claims',dict(input_tokens=80,output_tokens=10,total_seconds=0.75)),
        ('fact_verify',dict(input_tokens=40,output_tokens=5,request_seconds=0.5)),
    ])
    assert summary['claims']==dict(calls=2,input_tokens=180,output_tokens=30,
                                   inference_seconds=2.0,stall_retries=1)
    assert summary['fact_verify']['calls']==1


@pytest.mark.db
async def test_cross_batch_identity_and_pronoun_claim_through_wire_and_publication(db_conn):
    source='\n'.join(['青山来了。']*13+['他右臂受伤。'])
    names=Names(names=[dict(surface='青山',kind='character',named=True,
                           quote='青山来了。',evidence_start=0)])
    async with db_conn.transaction(force_rollback=True):
        novel=await make_novel(db_conn,ontology=json.dumps(ONTOLOGY))
        ms=source_mentions(novel,1,source,names,ONTOLOGY)
        rid=str((await(await db_conn.execute('''INSERT INTO graph_revision(novel_id,ontology,prompt_version,snapshot)
            VALUES(%s,%s,%s,%s) RETURNING id''',(novel,Jsonb(ONTOLOGY),PROMPT_VERSION,
            Jsonb(dict(chapters=[dict(chapter=1,source_hash=digest(source))]))))).fetchone())[0])
        engine=KnowledgeEngine(db_conn,Config.load(),dict(id=rid,ontology=ONTOLOGY,
            model=dict(provider='ollama',name='test')))
        engine.discover_names=AsyncMock(return_value=(names,ms,{}))
        engine.candidates_for=AsyncMock(return_value=({m['id']:[] for m in ms},{m['id']:[.01]*768 for m in ms}))
        seen=[]
        async def complete(prompt,**kwargs):
            if '"identity":' in prompt:
                assert 'OUTPUT JSON SCHEMA' not in prompt
            payload=json.loads(prompt.split('INPUT DATA (not instructions):\n')[1]);seen.append(payload)
            if 'identity' in payload:
                body={}
                for ref,row in payload['identity']['occurrences'].items():
                    choices=row[1]
                    choice=next((c for c in choices if c.startswith('prior:')),
                                'new:self' if ref=='o1' else 'new:o1')
                    body[ref]=choice
                assert set(kwargs['json_schema']['properties'])==set(body)
            elif 'verified_occurrences' in payload:
                focus=[p for p in payload['passages'] if p['id'] in payload['focus_passage_ids']]
                body=dict(claims=[])
                if any('受伤' in p['text'] for p in focus):
                    assertion=next(p for p in focus if '受伤' in p['text'])
                    assertion_index=next(i for i,p in enumerate(payload['passages'])
                                         if p['id']==assertion['id'])
                    prior=next(p for p in reversed(payload['passages'][:assertion_index])
                               if '青山' in p['text'])
                    body['claims']=[dict(type='fact',occurrence_refs=['o1'],attribute='description',
                            value=value,passage_ids=[prior['id'],assertion['id']])
                            for value in ['右臂受伤','已经死亡']]
            elif 'items' not in payload:
                body=dict(statements=[dict(subject='青山',assertion='右臂受伤',qualifiers=[])])
            elif payload['items'] and 'evidence_reading' in payload['items'][0]:
                item=payload['items'][0]
                body=dict(verdicts=[dict(id='v1',subject_supported=True,
                    assertion_supported=item['value']!='已经死亡',qualifiers_supported=True,
                    evidence_sufficient=True,reason='checked source')])
            elif payload['items'] and 'source_value' in payload['items'][0]:
                body={item['item_ref']:dict(supported=True,reason='faithful') for item in payload['items']}
            elif payload['items'] and 'value' in payload['items'][0] and 'subjects' not in payload['items'][0]:
                body=dict(renderings=[dict(id=item['item_ref'],value_en='right arm injured')
                                      for item in payload['items']])
            else:
                body={}
                for item in payload['items']:
                    if 'outcome' in item:
                        assert item['subject']['surface']==item['target']['surface']=='青山'
                        assert item['subject']['kind']==item['target']['kind']=='character'
                    else:
                        assert item['subjects'][0]['surface']=='青山'
                        assert '青山' in item['quote'] and '他右臂受伤' in item['quote']
                    body[item['item_ref']]=dict(
                        supported=item.get('value')!='已经死亡',reason='checked source')
                assert '$defs' not in kwargs['json_schema']
                assert kwargs['json_schema']['required']==[item['item_ref'] for item in payload['items']]
            return Completion(text=json.dumps(body),served_provider='ollama',served_model='test')
        engine.provider.complete=complete
        try:
            output=await engine.extract(novel,1,source,'','en')
            assert output['diagnostics']['verified_identities']==13
            assert output['diagnostics']['verified_claims']==1
            identity_payloads=[payload for payload in seen if 'identity' in payload]
            assert [len(payload['identity']['occurrences']) for payload in identity_payloads]==[12,1]
            await db_conn.execute("INSERT INTO glossary(novel_id,source_term,target_term,locked_at_chapter,constraint_class) VALUES(%s,'青山','Qing Shan',0,'character_name')",(novel,))
            await db_conn.execute("SELECT set_config('app.graph_revision',%s,true),set_config('app.graph_generation','1',true)",(rid,))
            state=PipelineState(envelope=ChapterEnvelope(novel_id=novel,chapter_index=1,source_lang='zh',raw_text=source))
            await engine.publish(state,output,source)
            assert len(set(state.resolutions.values()))==1
            facts=await(await db_conn.execute('SELECT value FROM fact WHERE revision_id=%s',(rid,))).fetchall()
            assert facts==[('右臂受伤',)]
            assert output['diagnostics']['published_facts']==1
        finally:
            await engine.close()


async def test_subdivision_preserves_parent_antecedent_ranges():
    from pipeline.evidence import ClaimProposals
    source='青山来了。\n'+'他做事。'*110
    m=dict(id='m',surface='青山',kind='character',char_start=0,char_end=2,quote='青山来了。')
    engine=object.__new__(KnowledgeEngine);seen=[]
    async def call(stage,schema,payload):
        seen.append(payload)
        n=12 if payload['_passage_max_chars']==400 else 0
        return ClaimProposals(claims=[dict(type='fact',occurrence_refs=['o1'],attribute='description',
            value=str(i),quote=source,evidence_start=0) for i in range(n)])
    engine.call=call
    focus=source_passages(source,max_chars=400,overlap=0)[1]
    await engine._claims_for_focus(source,[m],ONTOLOGY,focus,passage_chars=400)
    assert len(seen)>1
    assert all(p['verified_occurrences'][0]['surface']=='青山' for p in seen)
    # The top-level call re-slices nothing, so its context IS its offered passages;
    # synthesizing ranges there would offer the focus text under a second ID.
    assert seen[0]['_context_ranges']==[]
    antecedent=(0,len('青山来了。'))
    for p in seen[1:]:
        ranges=p['_context_ranges']
        # The parent antecedent survives subdivision — that is why these ranges exist.
        assert any(lo<=antecedent[0] and antecedent[1]<=hi for lo,hi in ranges)
        # ...but never as a second way to cite the assertion this call must ground in
        # its own focus passage.
        offered=[q for q in source_passages(source,max_chars=p['_passage_max_chars'],overlap=0)
                 if q['id']==p['focus_passage_ids'][0]]
        start,end=offered[0]['char_start'],offered[0]['char_end']
        assert all(hi<=start or lo>=end for lo,hi in ranges)


async def test_default_claim_window_offers_broader_neighbor_context():
    from pipeline.evidence import ClaimProposals
    first='青山来到山门。'+'甲'*500
    middle='他向守卫出示令牌。'+'乙'*500
    last='守卫打开大门。'+'丙'*500
    source='\n'.join([first,middle,last])
    mention=dict(id='m',surface='青山',kind='character',char_start=0,char_end=2,quote=first)
    focus=source_passages(source,max_chars=1200,overlap=0)[1]
    engine=object.__new__(KnowledgeEngine);seen=[]
    async def call(_stage,_schema,payload):
        seen.append(payload)
        return ClaimProposals(claims=[])
    engine.call=call
    await engine._claims_for_focus(source,[mention],ONTOLOGY,focus)
    assert len(seen)==1
    payload=seen[0]
    assert payload['_passage_max_chars']==1200
    offered={p['id'] for p in source_passages(source,max_chars=1200,overlap=0)}
    assert set(payload['_passage_ids'])==offered
    assert payload['verified_occurrences'][0]['surface']=='青山'


async def test_oversized_claim_context_splits_before_inference():
    from pipeline.evidence import ClaimProposals
    source='青山做事。'+'甲'*900
    mention=dict(id='m',surface='青山',kind='character',char_start=0,char_end=2,quote=source[:5])
    focus=source_passages(source,max_chars=1200,overlap=0)[0]
    engine=object.__new__(KnowledgeEngine);seen=[]
    async def call(_stage,_schema,payload):
        seen.append(payload['_passage_max_chars'])
        if payload['_passage_max_chars']>600:
            raise ValueError('graph context exceeds hard model budget; bounded caller contract regressed')
        return ClaimProposals(claims=[])
    engine.call=call
    await engine._claims_for_focus(source,[mention],ONTOLOGY,focus)
    assert seen[0]==1200 and all(size==600 for size in seen[1:])
    assert engine._claim_context_splits==1


async def test_oversized_identity_context_halves_batch_and_records_sizes():
    from pipeline.evidence import IdentityDecisions, Verification
    lines=[f'青山{i}来了。' for i in range(7)]
    source='\n'.join(lines);mentions=[];offset=0
    for i,line in enumerate(lines):
        surface=f'青山{i}'
        mentions.append(dict(id=f'm{i}',surface=surface,kind='character',
            char_start=offset,char_end=offset+len(surface),quote=line))
        offset+=len(line)+1
    engine=object.__new__(KnowledgeEngine)
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
    engine=object.__new__(KnowledgeEngine);offered=[]
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
    engine=object.__new__(KnowledgeEngine)
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


def _claims_engine(recorded=None, cached=None):
    """A small engine double whose cache reads and bookkeeping writes are captured."""
    class Cursor:
        async def fetchone(self): return cached if cached is not None else None
    class DB:
        async def execute(self,sql,params=None,*_a,**_k):
            if recorded is not None:
                recorded.append((sql,params))
            if cached is not None and 'FROM completion_cache' in sql:
                return Cursor()
            return Cursor()
    engine=KnowledgeEngine(DB(),Config.load(),dict(id='test',ontology=ONTOLOGY,
        model=dict(provider='ollama',name='test')))
    engine.run_id,engine.current_chapter,engine._novel_id='run','1','novel'
    return engine


async def test_cache_hit_rematerializes_and_refilters_raw_claim_wire_response():
    """A cache hit must obey the current focus and adjacency contract."""
    from pipeline.evidence import ClaimProposals
    source='凌峰来了。\n他拿起地图。\n她离开房间。'
    passages=source_passages(source,max_chars=400,overlap=0)
    raw=dict(claims=[dict(type='fact',occurrence_refs=['o1'],attribute='description',
        value='凌峰拿起地图',passage_ids=[passages[0]['id'],passages[2]['id']])])
    recorded=[]
    engine=_claims_engine(recorded,cached=(raw,{'proposed_count':1},'ollama','test'))
    engine.provider.complete=AsyncMock()
    try:
        result=await engine.call('claims',ClaimProposals,dict(
            source=source,verified_occurrences=[dict(occurrence_ref='o1',surface='凌峰',
                kind='character',context=passages[0]['text'])],ontology=ONTOLOGY,
            focus_passage_ids=[passages[0]['id'],passages[2]['id']],
            _passage_ids=[passages[0]['id'],passages[2]['id']],_passage_max_chars=400,
            _passage_overlap=0,_batch_id='cached-non-adjacent'))
        assert result.claims==[]
        engine.provider.complete.assert_not_awaited()
        rejected=[params for sql,params in recorded
                   if 'chapter_knowledge_activity' in sql and params[5]=='rejected']
        assert len(rejected)==1
        assert rejected[0][6].obj['rejection']=='claim cites non-adjacent evidence passages'
    finally:
        await engine.close()


async def test_cache_stores_raw_wire_response_before_materialization():
    from pipeline.evidence import Names
    source='凌峰来到碎星滩。'
    pid=source_passages(source)[0]['id']
    wire=dict(reviewed={kind:True for kind in ONTOLOGY['kinds']},
              names=[dict(surface='凌峰',kind='character',passage_id=pid)])
    recorded=[]
    engine=_claims_engine(recorded)
    engine.provider.complete=AsyncMock(return_value=Completion(text=json.dumps(wire),
        served_provider='ollama',served_model='test'))
    try:
        result=await engine.call('names',Names,dict(source=source))
        assert result.names[0].quote==source
        inserts=[params for sql,params in recorded
                 if 'INSERT INTO completion_cache\n' in sql]
        assert len(inserts)==1
        stored=inserts[0][4]
        assert isinstance(stored,Jsonb) and stored.obj==wire
        assert 'quote' not in stored.obj['names'][0]
    finally:
        await engine.close()


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


async def test_top_level_claims_never_offer_two_ids_for_the_same_source_text():
    """The focus check is satisfiable only if the focus text has exactly one citable ID.

    Offering a synthetic duplicate let the model ground its assertion correctly and still
    cite the other ID, which failed the whole run.
    """
    from pipeline.evidence import ClaimProposals
    source='青山来了。\n'+'他做事。'*40+'\n'+'她走了。'*40
    m=dict(id='m',surface='青山',kind='character',char_start=0,char_end=2,quote='青山来了。')
    focus=source_passages(source,max_chars=400,overlap=0)[1]
    engine=_claims_engine()
    body=dict(claims=[dict(type='fact',occurrence_refs=['o1'],attribute='description',
        value='做事',passage_ids=[focus['id']])])
    engine.provider.complete=AsyncMock(return_value=Completion(text=json.dumps(body),
        served_provider='ollama',served_model='test'))
    try:
        await engine._claims_for_focus(source,[m],ONTOLOGY,focus,passage_chars=400)
        schema=engine.provider.complete.call_args.kwargs['json_schema']
        offered=schema['$defs']['ClaimProposal']['properties']['passage_ids']['items']['enum']
        by_id={p['id']:p for p in source_passages(source,max_chars=400,overlap=0)}
        assert focus['id'] in offered
        assert all(ref in by_id for ref in offered), 'no synthetic duplicate of an offered slice'
        spans=[(by_id[ref]['char_start'],by_id[ref]['char_end']) for ref in offered]
        assert len(set(spans))==len(spans)
        assert not [ref for ref in offered
                    if ref!=focus['id'] and focus['text'] in by_id[ref]['text']]
    finally:
        await engine.close()


async def test_claim_citing_only_context_is_rejected_without_failing_the_run():
    from pipeline.evidence import ClaimProposals
    source='青山来了。\n'+'他做事。'*40+'\n'+'她走了。'*40
    m=dict(id='m',surface='青山',kind='character',char_start=0,char_end=2,quote='青山来了。')
    passages=source_passages(source,max_chars=400,overlap=0)
    focus=passages[1];antecedent=passages[0]
    recorded=[]
    engine=_claims_engine(recorded)
    body=dict(claims=[
        dict(type='fact',occurrence_refs=['o1'],attribute='description',
             value='做事',passage_ids=[focus['id']]),
        dict(type='fact',occurrence_refs=['o1'],attribute='description',
             value='来了',passage_ids=[antecedent['id']])])
    engine.provider.complete=AsyncMock(return_value=Completion(text=json.dumps(body),
        served_provider='ollama',served_model='test'))
    try:
        claims,rejected,proposed=await engine._claims_for_focus(
            source,[m],ONTOLOGY,focus,passage_chars=400)
        # One bad citation costs one claim, not the whole chapter's extraction.
        assert [c.value for c in claims]==['做事']
        # Saturation is measured on what the model emitted, not on what survived.
        assert proposed==2
        activity=[params for sql,params in recorded
                  if 'chapter_knowledge_activity' in sql and params[5]=='rejected']
        assert len(activity)==1 and activity[0][3]=='fact'
        assert 'no focus passage' in activity[0][6].obj['rejection']
    finally:
        await engine.close()


async def test_non_adjacent_claim_is_soft_rejected_and_audit_keeps_all_citations():
    from pipeline.evidence import ClaimProposals
    source='凌峰来了。\n他拿起地图。\n她离开房间。'
    passages=source_passages(source,max_chars=400,overlap=0)
    recorded=[]
    engine=_claims_engine(recorded)
    body=dict(claims=[dict(type='fact',occurrence_refs=['o1'],attribute='description',
        value='凌峰拿起地图',passage_ids=[passages[0]['id'],passages[2]['id']])])
    engine.provider.complete=AsyncMock(return_value=Completion(text=json.dumps(body),
        served_provider='ollama',served_model='test'))
    try:
        request=dict(source=source,verified_occurrences=[dict(occurrence_ref='o1',
            surface='凌峰',kind='character',context=passages[0]['text'])],ontology=ONTOLOGY,
            focus_passage_ids=[passages[0]['id'],passages[2]['id']],
            # The cited IDs are neighbors in this filtered offer; source order still
            # contains an uncited passage between them and must drive rejection.
            _passage_ids=[passages[0]['id'],passages[2]['id']],_passage_max_chars=400,
            _passage_overlap=0,_batch_id='claims-non-adjacent')
        result=await engine.call('claims',ClaimProposals,request)
        assert result.claims==[]
        activity=[params for sql,params in recorded
                  if 'chapter_knowledge_activity' in sql and params[5]=='rejected']
        assert len(activity)==1 and activity[0][3]=='fact'
        audited=activity[0][6].obj
        assert audited['passage_ids']==[passages[0]['id'],passages[2]['id']]
        assert audited['rejection']=='claim cites non-adjacent evidence passages'
    finally:
        await engine.close()


def test_hosted_claim_batches_use_one_ordinary_request_and_scale_with_chapter_size():
    engine=object.__new__(KnowledgeEngine);engine.hosted=True
    assert len(engine._claim_batches('甲'*8_000))==1
    batches=engine._claim_batches('甲'*50_000)
    assert len(batches)==3
    assert all(sum(len(row['text']) for row in batch)<=24_000 for batch in batches)


async def test_hosted_fact_review_batches_verification_and_target_rendering_once():
    from pipeline.evidence import HostedFactReviewVerdict, HostedFactReviews
    source='青山很强。青山住在山上。'
    mention=dict(id='m1',surface='青山',kind='character',char_start=0,char_end=2,
                 quote=source)
    items=[
        dict(id='claim:1',type='fact',mention_ids=['m1'],attribute='description',
             value='很强',value_en='',quote=source,evidence_start=0),
        dict(id='claim:2',type='fact',mention_ids=['m1'],attribute='description',
             value='住在山上',value_en='',quote=source,evidence_start=0),
    ]
    engine=object.__new__(KnowledgeEngine)
    engine._activity=AsyncMock()
    engine.call=AsyncMock(return_value=HostedFactReviews(verdicts=[
        HostedFactReviewVerdict(id='v1',subject_supported=True,assertion_supported=True,
            qualifiers_supported=True,evidence_sufficient=True,reason='supported',
            value_target='is strong'),
        HostedFactReviewVerdict(id='v2',subject_supported=True,assertion_supported=False,
            qualifiers_supported=True,evidence_sufficient=True,reason='location is ambiguous',
            value_target='lives on the mountain'),
    ]))
    accepted,rejected=await engine._review_hosted_claims(
        source,items,[mention],'en')
    assert [item['value_en'] for item in accepted]==['is strong']
    assert len(rejected)==1 and 'assertion_supported' in rejected[0]['rejection']
    engine.call.assert_awaited_once()
    assert engine.call.await_args.args[:2]==('fact_review',HostedFactReviews)
    assert engine.call.await_args.args[2]['target_language']=='en'


def _engine(limit=1):
    """A bare engine carrying only the scheduling primitives `_fan_out` reads."""
    import asyncio
    engine=object.__new__(KnowledgeEngine)
    engine.max_concurrent_calls=limit
    engine._call_slots=asyncio.Semaphore(limit)
    engine._db_lock=asyncio.Lock()
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
    engine=_engine();offered=[];probes=[]
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
