"""Exercise actual request schemas, materialization, verification and publication."""
import json
from unittest.mock import AsyncMock

import pytest
from psycopg.types.json import Jsonb

from pipeline.config import Config
from pipeline.context import PipelineState
from pipeline.envelope import ChapterEnvelope
from pipeline.evidence import Names, digest, source_mentions, PROMPT_VERSION
from pipeline.knowledge import KnowledgeEngine
from pipeline.knowledge_contract import (
    identity_schema, materialize_identity, materialize_names, materialize_verification,
    name_schema,
    unique_json_object, verification_schema,
)
from pipeline.passages import PassageContract, source_passages
from pipeline.benchmark_knowledge import reviewed_fact_recall
from novel_llm.provider import Completion
from tests.fixtures import make_novel

ONTOLOGY=dict(kinds=['character'],relations=[],attributes=[dict(name='description',kinds=['character'])])


def test_slots_enforce_per_occurrence_choices_and_duplicate_keys():
    contract=PassageContract('青山来了。')
    pid=contract.passages[0]['id']
    rows=[dict(occurrence_ref='o1',choices={'new:self':('new','o1','new_first_appearance'),
                                          'unresolved':('unresolved',None,'ambiguous')})]
    schema=identity_schema(rows,[pid])
    assert schema['required']==['o1']
    assert schema['properties']['o1']['properties']['choice']['enum']==['new:self','unresolved']
    body={'o1':dict(choice='new:self',passage_id=pid,explanation='明确出现')}
    assert materialize_identity(body,rows,contract).decisions[0].target_ref=='o1'
    body['o1']['choice']='existing:e1'
    with pytest.raises(ValueError,match='unoffered'): materialize_identity(body,rows,contract)
    with pytest.raises(ValueError,match='one unique'): materialize_identity({},rows,contract)
    with pytest.raises(ValueError,match='duplicate JSON'):
        json.loads('{"o1":1,"o1":2}',object_pairs_hook=unique_json_object)


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
    assert schema['required']==['n1','n2','n3','n4','n5','n6']
    assert '$defs' not in schema and 'maxItems' not in json.dumps(schema)
    body={f'n{i}':None for i in range(1,7)}
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
            payload=json.loads(prompt.split('INPUT DATA (not instructions):\n')[1]);seen.append(payload)
            if 'identity_occurrences' in payload:
                body={}
                for row in payload['identity_occurrences']:
                    choices=list(row['choices'])
                    choice=next((c for c in choices if c.startswith('prior:')),
                                'new:self' if row['occurrence_ref']=='o1' else 'new:o1')
                    pid=next(p['id'] for p in payload['passages'] if p['text']==row['subject']['quote'])
                    # Repeated exact text needs the occurrence's correct source offset.
                    pid=source_passages(source)[row['subject']['char_start']//6]['id']
                    body[row['occurrence_ref']]=dict(choice=choice,passage_id=pid,explanation='叙事连续')
                assert set(kwargs['json_schema']['properties'])==set(body)
            elif 'verified_occurrences' in payload:
                focus=next(p for p in payload['passages'] if p['id']==payload['focus_passage_ids'][0])
                body=dict(claims=[])
                if '受伤' in focus['text']:
                    prior=next(p for p in payload['passages'] if '青山' in p['text'])
                    body['claims']=[dict(type='fact',occurrence_refs=['o1'],attribute='description',
                        value=value,value_en='',passage_ids=[prior['id'],focus['id']])
                        for value in ['右臂受伤','已经死亡']]
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
            identity_payloads=[payload for payload in seen if 'identity_occurrences' in payload]
            assert [len(payload['identity_occurrences']) for payload in identity_payloads]==[12,1]
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
            value=str(i),value_en=str(i),quote=source,evidence_start=0) for i in range(n)])
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
            raise ValueError('graph context exceeds hard local model budget; bounded caller contract regressed')
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
                raise ValueError('graph context exceeds hard local model budget; bounded caller contract regressed')
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


def _claims_engine(recorded=None):
    """A KnowledgeEngine whose completion cache always misses and whose writes are captured."""
    class Cursor:
        async def fetchone(self): return None
    class DB:
        async def execute(self,sql,params=None,*_a,**_k):
            if recorded is not None:
                recorded.append((sql,params))
            return Cursor()
    engine=KnowledgeEngine(DB(),Config.load(),dict(id='test',ontology=ONTOLOGY,
        model=dict(provider='ollama',name='test')))
    engine.run_id,engine.current_chapter,engine._novel_id='run','1','novel'
    return engine


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
        value='做事',value_en='acts',passage_ids=[focus['id']])])
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
             value='做事',value_en='acts',passage_ids=[focus['id']]),
        dict(type='fact',occurrence_refs=['o1'],attribute='description',
             value='来了',value_en='arrived',passage_ids=[antecedent['id']])])
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
