"""Focused vocabulary runtime contract tests (Phase C.3-C.8)."""
import json
import hashlib

import pytest
from psycopg.types.json import Jsonb

from pipeline.evidence import Claim, ExtractProposals, Proposals, Names, validate_proposals
from pipeline.knowledge import KnowledgeEngine
from pipeline.passages import PassageContract
from pipeline.vocabulary import normalize_name, valid_name, record_candidate, vocabulary_prompt
from tests.fixtures import make_novel


def test_vocabulary_normalization_is_syntactic_only():
    assert normalize_name('  Hair / Color ') == 'hair_color'
    assert valid_name('hair_color')
    assert not valid_name('1hair')
    assert not valid_name('a')
    assert not valid_name('a' * 41)
    assert normalize_name('Looks!') == 'looks!'


def test_extract_schema_keeps_vocabulary_attribute_as_plain_string():
    contract = PassageContract('凌峰来了。')
    schema = contract.schema('extract', ExtractProposals,
        {'kinds':['character'], 'attributes':[], 'relations':[]},
        {'attributes':[{'name':'appearance','gloss':'durable physical description'}],
         'relations':[{'name':'ally','gloss':'durable relationship'}]})
    attr = schema['properties']['attributes']['items']['properties']['attribute']
    assert attr == {'type': 'string'}
    prompt = vocabulary_prompt([dict(term_type='attribute',name='appearance',gloss='durable')])
    assert prompt['attributes'] == [dict(name='appearance',gloss='durable')]


def test_plain_wire_vocabulary_names_are_checked_locally():
    """The provider sees one string type; local validation owns the naming rule."""
    source = '凌峰来了。'
    mention = {'id': 'm1', 'surface': '凌峰', 'kind': 'character',
               'char_start': 0, 'char_end': 2}
    claim = Claim(type='fact', mention_ids=['m1'], attribute='Bad Term', value='来了',
                  quote=source, evidence_start=0)
    accepted, rejected = validate_proposals(
        source, [mention], {}, Proposals(decisions=[], claims=[claim]),
        {'kinds': ['character'], 'attributes': [], 'relations': []}, vocabulary={}
    )
    assert accepted == []
    assert rejected[0]['rejection'] == 'invalid attribute or entity kind'


@pytest.mark.asyncio
async def test_embedding_suggestions_are_read_only_and_bounded():
    from pipeline.vocabulary import embedding_suggestions
    class Cursor:
        async def fetchall(self):
            return [('appearance','admitted',0.08)]
    class DB:
        def __init__(self): self.sql=[]
        async def execute(self, sql, params=None):
            self.sql.append(sql); return Cursor()
    class Embedder:
        async def embed(self, values): return [[0.1,0.2]]
    db=DB()
    result=await embedding_suggestions(db,Embedder(),'n','attribute','looks',limit=99)
    assert result == [dict(name='appearance',status='admitted',distance=0.08)]
    assert all('INSERT' not in sql and 'UPDATE' not in sql for sql in db.sql)


@pytest.mark.db
async def test_vocabulary_votes_are_distinct_chapters_and_admit(db_conn):
    async with db_conn.transaction(force_rollback=True):
        novel = await make_novel(db_conn)
        first = await record_candidate(db_conn, novel, 'attribute', 'looks', 'character', 3)
        again = await record_candidate(db_conn, novel, 'attribute', 'looks', 'character', 3)
        assert first['proposals'] == again['proposals'] == 1
        admitted = await record_candidate(db_conn, novel, 'attribute', 'looks', 'character', 7)
        assert admitted['status'] == 'admitted' and admitted['proposals'] == 2
        assert (await (await db_conn.execute('''SELECT count(*) FROM novel_vocabulary_changelog
            WHERE novel_id=%s AND name='looks' AND action='admit' ''',(novel,))).fetchone())[0] == 1


@pytest.mark.db
async def test_vocabulary_relation_records_src_dst_and_tombstone_wins(db_conn):
    async with db_conn.transaction(force_rollback=True):
        novel = await make_novel(db_conn)
        await record_candidate(db_conn, novel, 'relation', 'trusts', 'character', 3,
                               dst_kind='place')
        row = await (await db_conn.execute('''SELECT kinds,dst_kinds FROM novel_vocabulary
            WHERE novel_id=%s AND term_type='relation' AND name='trusts' ''',(novel,))).fetchone()
        assert 'character' in row[0] and 'place' in row[1]
        await db_conn.execute('''UPDATE novel_vocabulary SET status='banned'
            WHERE novel_id=%s AND term_type='relation' AND name='trusts' ''',(novel,))
        before = await (await db_conn.execute('''SELECT kinds,dst_kinds,proposals,last_seen_chapter
            FROM novel_vocabulary WHERE novel_id=%s AND term_type='relation' AND name='trusts' ''',(novel,))).fetchone()
        result = await record_candidate(db_conn, novel, 'relation', 'trusts', 'group', 9,
                                        dst_kind='group')
        after = await (await db_conn.execute('''SELECT kinds,dst_kinds,proposals,last_seen_chapter
            FROM novel_vocabulary WHERE novel_id=%s AND term_type='relation' AND name='trusts' ''',(novel,))).fetchone()
        assert result['status'] == 'banned' and after == before


@pytest.mark.db
async def test_vocabulary_resolution_is_chapter_visible(db_conn):
    from pipeline.vocabulary import resolve
    async with db_conn.transaction(force_rollback=True):
        # The seed trigger only seeds default vocabulary for kinds the novel's own
        # ontology declares (C.1) -- an empty ontology seeds nothing.
        novel = await make_novel(db_conn, ontology=json.dumps({'kinds':['character'],'attributes':[],'relations':[]}))
        assert (await resolve(db_conn, novel, 'attribute', 'appearance', 0))['status'] == 'admitted'
        await db_conn.execute('''INSERT INTO novel_vocabulary_alias
            (novel_id,term_type,surface,name,known_from_chapter,created_by)
            VALUES(%s,'attribute','looks','appearance',10,'test')''',(novel,))
        before = await resolve(db_conn, novel, 'attribute', 'looks', 5)
        after = await resolve(db_conn, novel, 'attribute', 'looks', 10)
        assert before['status'] == 'unknown' and after['name'] == 'appearance'
        await db_conn.execute('''INSERT INTO novel_vocabulary
            (novel_id,term_type,name,kinds,status,gloss,first_seen_chapter,last_seen_chapter,admitted_at_chapter)
            VALUES(%s,'attribute','future_term',ARRAY['character'],'admitted','future',12,12,12)''',(novel,))
        assert (await resolve(db_conn,novel,'attribute','future_term',5))['status'] == 'unknown'


@pytest.mark.db
async def test_unknown_claims_are_canonicalized_and_recorded(db_conn):
    from pipeline.knowledge import KnowledgeEngine
    from pipeline.evidence import validate_proposals
    from pipeline.vocabulary import normalize_name
    async with db_conn.transaction(force_rollback=True):
        novel=await make_novel(db_conn)
        rid=str((await (await db_conn.execute(
            "INSERT INTO graph_revision(novel_id,ontology) VALUES(%s,%s) RETURNING id",
            (novel,Jsonb({'kinds':['character','place'],'attributes':[],'relations':[]})))).fetchone())[0])
        engine=object.__new__(KnowledgeEngine); engine.db=db_conn; engine.revision={'id':rid,'novel_id':novel}; engine._novel_id=novel; engine.current_chapter=3
        mentions=[dict(id='m1',kind='character',surface='凌峰',char_start=0,char_end=2),
                  dict(id='m2',kind='place',surface='山门',char_start=3,char_end=5)]
        claims=[Claim(type='fact',mention_ids=['m1'],attribute='Looks',value='很高',quote='凌峰很高。',evidence_start=0),
                Claim(type='relationship',mention_ids=['m1','m2'],attribute='Trusts',value='信任',quote='凌峰信任山门。',evidence_start=5)]
        canonical, rows, unknown = await engine._canonicalize_vocabulary_claims(claims,mentions)
        accepted,rejected=validate_proposals('凌峰很高。凌峰信任山门。',mentions,{},
            Proposals(decisions=[],claims=canonical),{'attributes':[],'relations':[]},vocabulary=rows)
        await engine._record_accepted_vocabulary_candidates(accepted, unknown, mentions)
        assert len(accepted)==2 and not rejected
        row=await (await db_conn.execute('''SELECT kinds,dst_kinds FROM novel_vocabulary
            WHERE novel_id=%s AND name='trusts' ''',(novel,))).fetchone()
        assert 'character' in row[0] and 'place' in row[1]


@pytest.mark.db
async def test_banned_and_retired_terms_reject_without_mutation(db_conn):
    from pipeline.knowledge import KnowledgeEngine
    from pipeline.evidence import validate_proposals
    async with db_conn.transaction(force_rollback=True):
        novel=await make_novel(db_conn)
        rid=str((await (await db_conn.execute(
            "INSERT INTO graph_revision(novel_id,ontology) VALUES(%s,%s) RETURNING id",
            (novel,Jsonb({'kinds':['character'],'attributes':[],'relations':[]})))).fetchone())[0])
        for name,status in [('bad_term','banned'),('old_term','retired')]:
            await db_conn.execute('''INSERT INTO novel_vocabulary
                (novel_id,term_type,name,kinds,status,first_seen_chapter,last_seen_chapter)
                VALUES(%s,'attribute',%s,ARRAY['character'],%s,0,0)''',(novel,name,status))
        engine=object.__new__(KnowledgeEngine); engine.db=db_conn; engine.revision={'id':rid,'novel_id':novel}; engine._novel_id=novel; engine.current_chapter=3
        mentions=[dict(id='m',kind='character',surface='凌峰',char_start=0,char_end=2)]
        claims=[Claim(type='fact',mention_ids=['m'],attribute='bad_term',value='x',quote='凌峰。',evidence_start=0),
                Claim(type='fact',mention_ids=['m'],attribute='old_term',value='x',quote='凌峰。',evidence_start=0)]
        canonical,rows,_=await engine._canonicalize_vocabulary_claims(claims,mentions)
        accepted,rejected=validate_proposals('凌峰。',mentions,{},Proposals(decisions=[],claims=canonical),
            {'attributes':[],'relations':[]},vocabulary=rows)
        assert not accepted and len(rejected)==2


def test_vocabulary_hash_matches_go_golden():
    payload='["9305a18f-1617-4e41-a6d2-3877df98eb7e",7,"attribute","appearance","admit",12,"abc"]'
    got=hashlib.sha256(('abc'+payload).encode()).hexdigest()
    assert got == '5b2862d780b01c797519f1c8ad17a779217825aef4ed799547fc70e00cd11eb3'


def test_description_duplicate_guard_and_warning():
    from pipeline.knowledge import KnowledgeEngine
    items=[
        dict(id='claim:1',type='fact',mention_ids=['m'],attribute='appearance',value='tall',evidence_start=10),
        dict(id='claim:2',type='fact',mention_ids=['m'],attribute='description',value='  TALL ',evidence_start=12),
        dict(id='claim:3',type='fact',mention_ids=['m'],attribute='description',value='quiet',evidence_start=20),
        dict(id='claim:4',type='fact',mention_ids=['m'],attribute='description',value='calm',evidence_start=30),
    ]
    kept,rejected,warning=KnowledgeEngine._filter_description_claims(items)
    assert [item['id'] for item in rejected] == ['claim:2']
    assert [item['id'] for item in kept] == ['claim:1','claim:3','claim:4']
    assert warning is True


def test_invalid_claim_cardinality_and_evidence_are_rejected_without_side_effects():
    from pipeline.evidence import Claim, validate_proposals
    mentions=[dict(id='m',kind='character',char_start=0,char_end=2)]
    claims=[Claim.model_construct(type='fact',mention_ids=[],attribute='looks',value='x',quote='凌峰。',evidence_start=0),
            Claim(type='relationship',mention_ids=['m'],attribute='trusts',value='x',quote='凌峰。',evidence_start=0),
            Claim(type='fact',mention_ids=['m'],attribute='looks',value='x',quote='not present',evidence_start=0)]
    accepted,rejected=validate_proposals('凌峰。',mentions,{},Proposals(decisions=[],claims=claims),
        {'attributes':[],'relations':[]},vocabulary={})
    assert accepted == [] and len(rejected) == 3


@pytest.mark.db
async def test_invalid_claims_never_record_candidates(db_conn):
    from pipeline.knowledge import KnowledgeEngine
    async with db_conn.transaction(force_rollback=True):
        novel=await make_novel(db_conn)
        rid=str((await (await db_conn.execute(
            "INSERT INTO graph_revision(novel_id,ontology) VALUES(%s,%s) RETURNING id",
            (novel,Jsonb({'kinds':['character','place'],'attributes':[],'relations':[]})))).fetchone())[0])
        engine=object.__new__(KnowledgeEngine); engine.db=db_conn; engine.revision={'id':rid,'novel_id':novel}; engine._novel_id=novel; engine.current_chapter=3
        mentions=[dict(id='m1',kind='character',char_start=0,char_end=2),dict(id='m2',kind='place',char_start=2,char_end=4)]
        claims=[Claim(type='fact',mention_ids=[],attribute='looks',value='x',quote='凌峰。',evidence_start=0),
                Claim(type='relationship',mention_ids=['m1'],attribute='trusts',value='x',quote='凌峰。',evidence_start=0),
                Claim(type='fact',mention_ids=['m1'],attribute='looks',value='x',quote='absent',evidence_start=0)]
        canonical,rows,unknown=await engine._canonicalize_vocabulary_claims(claims,mentions)
        accepted,_=validate_proposals('凌峰。',mentions,{},Proposals(decisions=[],claims=canonical),
            {'attributes':[],'relations':[]},vocabulary=rows)
        await engine._record_accepted_vocabulary_candidates(accepted,unknown,mentions)
        assert (await (await db_conn.execute('SELECT count(*) FROM novel_vocabulary WHERE novel_id=%s',(novel,))).fetchone())[0] == 0
        assert (await (await db_conn.execute('SELECT count(*) FROM novel_vocabulary_chapter WHERE novel_id=%s',(novel,))).fetchone())[0] == 0


@pytest.mark.db
async def test_kind_widening_requires_two_chapters_and_bumps_once(db_conn):
    from pipeline.vocabulary import record_candidate
    async with db_conn.transaction(force_rollback=True):
        novel=await make_novel(db_conn)
        await db_conn.execute('''INSERT INTO novel_vocabulary
            (novel_id,term_type,name,kinds,dst_kinds,status,gloss,first_seen_chapter,last_seen_chapter,admitted_at_chapter)
            VALUES(%s,'relation','trusts',ARRAY['character'],ARRAY['place'],'admitted','',0,0,0)''',(novel,))
        # `novel`'s own initialize_graph_revision trigger already created one 'active'
        # row (empty ontology) -- widen that row in place and add the 'staging' peer,
        # rather than inserting a second 'active' row (would violate graph_one_active).
        ontology=Jsonb({'kinds':['character','group','place','sect'],'attributes':[],'relations':[]})
        await db_conn.execute('''UPDATE graph_revision SET ontology=%s,version=10
            WHERE novel_id=%s AND state='active' ''',(ontology,novel))
        await db_conn.execute('''INSERT INTO graph_revision(novel_id,ontology,state,version)
            VALUES(%s,%s,'staging',10)''',(novel,ontology))
        await record_candidate(db_conn,novel,'relation','trusts','group',3,dst_kind='sect')
        first=await (await db_conn.execute('SELECT kinds,dst_kinds FROM novel_vocabulary WHERE novel_id=%s AND name=\'trusts\'',(novel,))).fetchone()
        assert 'group' not in first[0] and 'sect' not in first[1]
        versions=await (await db_conn.execute('SELECT array_agg(version ORDER BY version) FROM graph_revision WHERE novel_id=%s',(novel,))).fetchone()
        assert versions[0] == [10,10]
        await record_candidate(db_conn,novel,'relation','trusts','group',3,dst_kind='sect')
        await record_candidate(db_conn,novel,'relation','trusts','group',7,dst_kind='sect')
        widened=await (await db_conn.execute('SELECT kinds,dst_kinds FROM novel_vocabulary WHERE novel_id=%s AND name=\'trusts\'',(novel,))).fetchone()
        assert 'group' in widened[0] and 'sect' in widened[1]
        versions=await (await db_conn.execute('SELECT array_agg(version ORDER BY version) FROM graph_revision WHERE novel_id=%s',(novel,))).fetchone()
        assert versions[0] == [11,11]


@pytest.mark.db
async def test_relation_destination_votes_are_role_tagged_and_one_chapter(db_conn):
    from pipeline.vocabulary import record_candidate
    async with db_conn.transaction(force_rollback=True):
        novel=await make_novel(db_conn)
        await record_candidate(db_conn,novel,'relation','links','character',3,dst_kind='place')
        await record_candidate(db_conn,novel,'relation','links','character',3,dst_kind='group')
        rows=await (await db_conn.execute('''SELECT kind FROM novel_vocabulary_chapter
            WHERE novel_id=%s AND term_type='relation' AND name='links' ORDER BY kind''',(novel,))).fetchall()
        assert [r[0] for r in rows] == ['character','dst:group','dst:place']
        assert (await (await db_conn.execute('''SELECT count(DISTINCT chapter_index) FROM novel_vocabulary_chapter
            WHERE novel_id=%s AND term_type='relation' AND name='links' ''',(novel,))).fetchone())[0] == 1


@pytest.mark.db
async def test_description_warning_activity_uses_allowed_phase(db_conn):
    async with db_conn.transaction(force_rollback=True):
        novel=await make_novel(db_conn)
        await db_conn.execute("INSERT INTO chapter(novel_id,chapter_index,raw_hash,raw_uri,source_meta,status) VALUES(%s,1,'x','x','{}','done')",(novel,))
        rid=str((await (await db_conn.execute("INSERT INTO graph_revision(novel_id,ontology) VALUES(%s,%s) RETURNING id",(novel,Jsonb({'kinds':['character']})))).fetchone())[0])
        run=str((await (await db_conn.execute('''INSERT INTO chapter_knowledge_run
            (novel_id,chapter_index,revision_id,mode,state,input_hash,display_hash,model_identity,graph_generation,graph_version)
            VALUES(%s,1,%s,'ordinary','processing','x','x','x',1,1) RETURNING id''',(novel,rid))).fetchone())[0])
        engine=object.__new__(KnowledgeEngine); engine.db=db_conn; engine.run_id=run; engine.current_chapter=1; engine._novel_id=novel
        await engine._activity('run','run','rejected',dict(warning='description exceeds 50% of facts',severity='warning'))
        row=await (await db_conn.execute('SELECT phase,payload->>\'severity\' FROM chapter_knowledge_activity WHERE run_id=%s',(run,))).fetchone()
        assert row == ('rejected','warning')


@pytest.mark.db
async def test_knowledge_extract_call_injects_only_chapter_visible_vocabulary(db_conn, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from pipeline.config import Config
    from pipeline.knowledge import KnowledgeEngine
    from novel_llm.provider import Completion
    monkeypatch.setenv('GRAPH_OLLAMA_FIRST_TOKEN_SECONDS','120')
    monkeypatch.setenv('GRAPH_OLLAMA_TIMEOUT_SECONDS','120')
    async with db_conn.transaction(force_rollback=True):
        novel=await make_novel(db_conn)
        await db_conn.execute('''INSERT INTO novel_vocabulary
            (novel_id,term_type,name,kinds,status,gloss,first_seen_chapter,last_seen_chapter,admitted_at_chapter)
            VALUES(%s,'attribute','visible_term',ARRAY['character'],'admitted','visible gloss',0,0,0),
                  (%s,'attribute','future_term',ARRAY['character'],'admitted','future gloss',10,10,10)
            ON CONFLICT DO NOTHING''',(novel,novel))
        revision=dict(id='00000000-0000-0000-0000-000000000001',novel_id=novel,
            prompt_version='evidence-v20-compact-identity-wire',
            ontology={'kinds':['character'],'attributes':[],'relations':[]},
            model={'provider':'anthropic','name':'alias','served_provider':'anthropic',
                   'served_model':'concrete'})
        response=Completion(text=json.dumps({'names':[], 'attributes':[], 'relations':[], 'occurrences':[]}),
                            served_provider='anthropic',served_model='concrete')
        complete=AsyncMock(return_value=response)
        provider=SimpleNamespace(complete=complete,aclose=AsyncMock())
        engine=KnowledgeEngine(db_conn,Config.load(),revision,provider=provider)
        engine.current_chapter=5
        source='凌峰很高。'
        try:
            await engine.call('extract',ExtractProposals,dict(
                source=source,ontology=revision['ontology'],
                _passage_ids=[],_passage_max_chars=400,_passage_overlap=0,
                _batch_id='vocab-visible'))
        finally:
            await engine.close()
        prompt=complete.await_args.args[0]
        assert 'OUTPUT JSON SCHEMA:' not in prompt
        schema=complete.await_args.kwargs['json_schema']
        assert 'visible_term' in prompt and 'visible gloss' in prompt
        assert 'future_term' not in prompt and 'future gloss' not in prompt
        attr=schema['properties']['attributes']['items']['properties']['attribute']
        assert attr == {'type': 'string'}
