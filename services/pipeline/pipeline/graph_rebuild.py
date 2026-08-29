"""Review-gated local graph repair. Never rewrites source, translations or glossary.

python -m pipeline.graph_rebuild prepare --novel UUID --model llama3.2:3b
python -m pipeline.graph_rebuild resume --revision UUID
python -m pipeline.graph_rebuild preview --revision UUID --output report.json
python -m pipeline.graph_rebuild activate --revision UUID --review-hash SHA256
python -m pipeline.graph_rebuild rollback --revision UUID

Activation is an explicit operator action and requires a frozen qualifying report.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import httpx
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from pipeline.config import Config
from pipeline.context import PipelineState
from pipeline.envelope import ChapterEnvelope, SourceMeta
from pipeline.evidence import PROMPT_VERSION, digest
from pipeline.knowledge import KnowledgeEngine
from pipeline.llm.provider import AdmissionRejected
from pipeline.stages.resolve import _lock_glossary


async def revision(db, rid, *, lock=False):
    async with db.cursor(row_factory=dict_row) as cur:
        await cur.execute('SELECT * FROM graph_revision WHERE id=%s'+(' FOR UPDATE' if lock else ''),(rid,))
        row = await cur.fetchone()
    if not row:
        raise ValueError('revision not found')
    row['id'],row['novel_id'] = str(row['id']),str(row['novel_id'])
    return row


def qualified(metrics: dict) -> bool:
    return (metrics.get('reviewed_mentions',0)>=60 and metrics.get('reviewed_facts',0)>=30
            and (metrics.get('link_precision') or 0)>=.98 and metrics.get('unambiguous_recall',0)>=.90
            and (metrics.get('fact_precision') or 0)>=.95 and metrics.get('merge_regressions',1)==0
            and metrics.get('evidence_valid',False) and metrics.get('reviewed',False)
            and metrics.get('publication_review_complete',False)
            and metrics.get('candidate_recall')==1)


def select_model(reports):
    candidates=[]
    for report in reports:
        m=report['metrics']
        if (report['model']['provider']=='ollama' and not m.get('failures')
            and m.get('tested_mentions',0)>=60 and m.get('tested_facts',0)>=30
            and m.get('model_inference_seconds') is not None
            and qualified(dict(m,publication_review_complete=True))):
            candidates.append(report)
    if not candidates:
        return None
    return min(candidates,key=lambda r:(-r['metrics']['link_precision'],-r['metrics']['fact_precision'],r['metrics']['model_inference_seconds']))['model']


async def local_model(cfg, name):
    from urllib.parse import urlparse
    if urlparse(cfg.ollama_host).hostname not in {'localhost','127.0.0.1','::1'}:
        raise ValueError('local Ollama required')
    async with httpx.AsyncClient(base_url=cfg.ollama_host) as client:
        response = await client.get('/api/tags')
        response.raise_for_status()
        matches = [m for m in response.json()['models'] if m['name']==name]
    if len(matches)!=1:
        raise ValueError('requested model is not installed; no automatic download or provider fallback')
    from pipeline.config import graph_runtime
    return dict(provider='ollama',name=name,digest=matches[0]['digest'],**graph_runtime(cfg))


def objects(cfg):
    from minio import Minio
    return Minio(cfg.object_endpoint,access_key=cfg.object_access_key,
                 secret_key=cfg.object_secret_key,secure=cfg.object_secure)


def read_object(client, cfg, uri):
    from urllib.parse import urlparse
    parsed = urlparse(uri)
    bucket,key = (parsed.netloc,parsed.path.lstrip('/')) if parsed.scheme=='s3' else (cfg.object_bucket,uri)
    response = client.get_object(bucket,key)
    try:
        return response.read().decode('utf-8')
    finally:
        response.close()
        response.release_conn()


async def prepare(db,cfg,novel,model):
    identity = await local_model(cfg,model)
    client = objects(cfg)
    row = await (await db.execute('SELECT ontology FROM novel WHERE id=%s',(novel,))).fetchone()
    if not row:
        raise ValueError('novel not found')
    ontology = row[0]
    ontology['kinds'] = list(dict.fromkeys(ontology['kinds']+['place','group']))
    ontology['attributes'] = [a for a in ontology['attributes'] if a['name']!='description']+[
        dict(name='description',kinds=ontology['kinds'])]
    # Snapshot only durably completed chapters; do not manipulate ordinary jobs.
    rows = await (await db.execute('''SELECT chapter_index,raw_uri,translated_uri,raw_hash FROM chapter
        WHERE novel_id=%s AND status='done' ORDER BY chapter_index''',(novel,))).fetchall()
    chapters = []
    for index,raw_uri,translated_uri,raw_hash in rows:
        source = await asyncio.to_thread(read_object,client,cfg,raw_uri)
        display = await asyncio.to_thread(read_object,client,cfg,translated_uri or raw_uri)
        chapters.append(dict(chapter=index,raw_uri=raw_uri,translated_uri=translated_uri,
                             raw_hash=raw_hash,source_hash=digest(source),display_hash=digest(display)))
    glossary = await (await db.execute('SELECT row_to_json(g) FROM glossary g WHERE novel_id=%s ORDER BY source_term',(novel,))).fetchall()
    progress = await (await db.execute('SELECT row_to_json(p) FROM reader_progress p WHERE novel_id=%s ORDER BY reader_id',(novel,))).fetchall()
    snapshot = dict(chapters=chapters,glossary=[g[0] for g in glossary],progress=[p[0] for p in progress])
    async with db.transaction():
        old = await (await db.execute('SELECT active_graph_revision FROM novel WHERE id=%s FOR UPDATE',(novel,))).fetchone()
        # Lock waits for a publication transaction to finish; future chapters cannot write.
        await db.execute('UPDATE graph_revision SET trusted=false,generation=generation+1,version=version+1 WHERE id=%s',(old[0],))
        rid = (await (await db.execute('''INSERT INTO graph_revision(novel_id,ontology,model,snapshot,prompt_version)
            VALUES(%s,%s,%s,%s,%s) RETURNING id''',(novel,Jsonb(ontology),Jsonb(identity),Jsonb(snapshot),PROMPT_VERSION))).fetchone())[0]
        await db.execute('INSERT INTO graph_audit(novel_id,revision_id,action,detail) VALUES(%s,%s,%s,%s)',
                         (novel,old[0],'quarantine',Jsonb(dict(replacement=str(rid)))))
        for c in chapters:
            await db.execute('''INSERT INTO graph_job(revision_id,chapter_index,input_hash,model_identity,generation)
                VALUES(%s,%s,%s,%s,1)''',(rid,c['chapter'],digest(c),digest(identity)))
    return str(rid)


def graph_retry_delay_minutes(attempt: int) -> int | None:
    return {1:5,2:15,3:45}.get(attempt)


async def resume(db,cfg,rid, *, limit=None):
    # Session advisory lock means interrupted jobs can be retried immediately, while
    # two resume processes cannot independently advance a revision out of order.
    locked = (await (await db.execute('SELECT pg_try_advisory_lock(hashtextextended(%s,0))',(rid,))).fetchone())[0]
    if not locked:
        raise RuntimeError('another worker is already enriching this revision')
    engine = None
    try:
        r = await revision(db,rid)
        if r['state']=='archived' or r['legacy']:
            raise ValueError('revision cannot be rebuilt')
        if await local_model(cfg,r['model']['name']) != r['model']:
            raise ValueError('installed model or inference configuration changed since snapshot; create a new revision')
        engine = KnowledgeEngine(db,cfg,r)
        client = objects(cfg)
        lang = await (await db.execute('SELECT source_lang,target_lang FROM novel WHERE id=%s',(r['novel_id'],))).fetchone()
        jobs = await (await db.execute('SELECT chapter_index FROM graph_job WHERE revision_id=%s AND state<>%s ORDER BY chapter_index',
                                      (rid,'done'))).fetchall()
        for (index,) in jobs[:limit] if limit else jobs:
            c = next(c for c in r['snapshot']['chapters'] if c['chapter']==index)
            source = await asyncio.to_thread(read_object,client,cfg,c['raw_uri'])
            display = await asyncio.to_thread(read_object,client,cfg,c['translated_uri'] or c['raw_uri'])
            if digest(source)!=c['source_hash'] or digest(display)!=c['display_hash']:
                raise ValueError('saved prose changed since snapshot')
            await db.execute("UPDATE graph_job SET state='processing',attempts=attempts+1,error=NULL,retry_at=NULL,generation=%s,updated_at=now() WHERE revision_id=%s AND chapter_index=%s",(r['generation'],rid,index))
            print(json.dumps(dict(revision=rid,chapter=index,state='processing')),flush=True)
            try:
                output = await engine.extract(r['novel_id'],index,source,display,lang[1])
                if await local_model(cfg,r['model']['name']) != r['model']:
                    raise ValueError('model or inference configuration changed during extraction; publication refused')
                state = PipelineState(envelope=ChapterEnvelope(novel_id=r['novel_id'],chapter_index=index,
                    raw_text=source,source_lang=lang[0],source_meta=SourceMeta()))
                async with db.transaction():
                    current = await revision(db,rid,lock=True)
                    if current['generation']!=r['generation'] or current['state']=='archived':
                        raise RuntimeError('worker fenced by revision change')
                    saved=await (await db.execute('SELECT raw_uri,translated_uri,raw_hash FROM chapter WHERE novel_id=%s AND chapter_index=%s FOR SHARE',
                        (r['novel_id'],index))).fetchone()
                    if saved!=(c['raw_uri'],c['translated_uri'],c['raw_hash']):
                        raise ValueError('chapter input changed during extraction')
                    await db.execute("SELECT set_config('app.graph_revision',%s,true),set_config('app.graph_generation',%s,true)",
                                     (rid,str(r['generation'])))
                    await engine.publish(state,output,source)
                    if current['state']=='active' and current['trusted']:
                        await promote_verified_glossary(db,current,index,lang[1])
                    await db.execute("UPDATE graph_job SET state='done',output=%s,retry_at=NULL,updated_at=now() WHERE revision_id=%s AND chapter_index=%s",
                                     (Jsonb(output),rid,index))
                    await db.execute('UPDATE graph_revision SET version=version+1,review=NULL WHERE id=%s',(rid,))
                print(json.dumps(dict(revision=rid,chapter=index,state='done',linked=len(state.resolutions))),flush=True)
            except AdmissionRejected:
                await db.execute("UPDATE graph_job SET state='pending',error=NULL,retry_at=NULL,updated_at=now() WHERE revision_id=%s AND chapter_index=%s",(rid,index))
                raise
            except Exception as exc:
                attempts=(await(await db.execute('SELECT attempts FROM graph_job WHERE revision_id=%s AND chapter_index=%s',(rid,index))).fetchone())[0]
                delay=graph_retry_delay_minutes(attempts)
                await db.execute("""UPDATE graph_job SET state='failed',error=%s,
                    retry_at=CASE WHEN %s::int IS NULL THEN NULL ELSE now()+(%s::int*interval '1 minute') END,
                    updated_at=now() WHERE revision_id=%s AND chapter_index=%s""",
                                 ((type(exc).__name__+': '+str(exc))[:2000],delay,delay,rid,index))
                raise
    finally:
        if engine:
            await engine.close()
        await db.execute('SELECT pg_advisory_unlock(hashtextextended(%s,0))',(rid,))


async def promote_verified_glossary(db,r: dict,chapter: int,target_lang: str) -> None:
    """Promote only verified alignments published by an active trusted revision."""
    if r.get('state')!='active' or not r.get('trusted') or r.get('legacy'):
        return
    rows=await (await db.execute('''SELECT p.source_term,p.target_term,
        array_agg(DISTINCT b.entity_id::text) FILTER (WHERE b.entity_id IS NOT NULL)
        FROM glossary_proposal_chapter p
        LEFT JOIN source_mention m ON m.revision_id=p.revision_id
          AND m.chapter_index=p.chapter_index AND m.surface=p.source_term
        LEFT JOIN mention_binding b ON b.revision_id=m.revision_id AND b.mention_id=m.id
        WHERE p.revision_id=%s AND p.chapter_index=%s
        GROUP BY p.source_term,p.target_term''',(r['id'],chapter))).fetchall()
    for source_term,target_term,entity_ids in rows:
        version=await _lock_glossary(db,novel_id=r['novel_id'],source_term=source_term,
            target_term=target_term,entity_id=None,chapter=chapter,target_lang=target_lang,
            min_proposals=2)
        if version is not None and entity_ids and len(entity_ids)==1:
            await db.execute('''INSERT INTO glossary_binding
                (novel_id,source_term,revision_id,entity_id,known_from_chapter)
                VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
                (r['novel_id'],source_term,r['id'],entity_ids[0],chapter))


async def preview(db,cfg,rid):
    r = await revision(db,rid)
    client = objects(cfg)
    checks = []
    sources={}
    for c in r['snapshot'].get('chapters',[]):
        source = await asyncio.to_thread(read_object,client,cfg,c['raw_uri'])
        sources[c['chapter']]=source
        display = await asyncio.to_thread(read_object,client,cfg,c['translated_uri'] or c['raw_uri'])
        saved=await (await db.execute('SELECT raw_uri,translated_uri,raw_hash FROM chapter WHERE novel_id=%s AND chapter_index=%s',
            (r['novel_id'],c['chapter']))).fetchone()
        checks.append(digest(source)==c['source_hash'] and digest(display)==c['display_hash']
                      and saved==(c['raw_uri'],c['translated_uri'],c['raw_hash']))
    jobs = await (await db.execute('SELECT chapter_index,state,error,output FROM graph_job WHERE revision_id=%s ORDER BY chapter_index',(rid,))).fetchall()
    claims = await (await db.execute('''SELECT e.canonical,f.attribute,f.value,f.source_chapter,v.quote,v.source_hash,v.char_start,v.char_end,f.id
        FROM fact f JOIN entity e ON e.id=f.entity_id LEFT JOIN graph_evidence v ON v.id=f.evidence_id
        WHERE f.revision_id=%s ORDER BY f.source_chapter,f.id''',(rid,))).fetchall()
    coverage = (await (await db.execute('''SELECT count(*),count(*) FILTER(WHERE EXISTS(SELECT 1 FROM mention_binding b
        WHERE b.revision_id=m.revision_id AND b.mention_id=m.id)) FROM source_mention m WHERE revision_id=%s''',(rid,))).fetchone())
    report = dict(revision=rid,generation=r['generation'],version=r['version'],model=r['model'],prompt_version=r['prompt_version'],
        ontology=r['ontology'],evaluation=r['evaluation'],saved_prose_unchanged=all(checks),
        completed=sum(j[1]=='done' for j in jobs),total_jobs=len(jobs),
        mention_coverage=dict(total=coverage[0],linked=coverage[1],unresolved=coverage[0]-coverage[1]),
        claims=[dict(zip(['canonical','attribute','value','chapter','quote','source_hash','start','end','id'],c)) for c in claims],
        failures=[dict(chapter=j[0],state=j[1],error=j[2]) for j in jobs if j[1]!='done'],
        rejected=[dict(chapter=j[0],**x) for j in jobs if j[3] for x in j[3].get('rejected',[])])
    splits=await(await db.execute('''SELECT a.surface,array_agg(DISTINCT old.canonical),array_agg(DISTINCT fresh.canonical)
        FROM alias a JOIN entity old ON old.id=a.entity_id JOIN graph_revision lr ON lr.id=old.revision_id AND lr.legacy
        JOIN alias b ON b.surface=a.surface AND b.revision_id=%s JOIN entity fresh ON fresh.id=b.entity_id
        WHERE old.novel_id=%s GROUP BY a.surface ORDER BY a.surface''',(rid,r['novel_id']))).fetchall()
    report['identity_changes']=[dict(source=s,legacy=old,replacement=new) for s,old,new in splits]
    glossary=await(await db.execute('SELECT row_to_json(g) FROM glossary g WHERE novel_id=%s ORDER BY source_term',(r['novel_id'],))).fetchall()
    report['glossary_unchanged']=[g[0] for g in glossary]==r['snapshot'].get('glossary',[])
    progress=await(await db.execute('SELECT row_to_json(p) FROM reader_progress p WHERE novel_id=%s ORDER BY reader_id',(r['novel_id'],))).fetchall()
    report['progress_matches_snapshot']=[p[0] for p in progress]==r['snapshot'].get('progress',[])
    report['progress_note']='Repair never writes reading progress; ordinary reading may advance it during rebuild.'
    evidence=await(await db.execute('SELECT chapter_index,source_hash,char_start,char_end,quote FROM graph_evidence WHERE revision_id=%s',(rid,))).fetchall()
    report['evidence_valid']=all(ch in sources and digest(sources[ch])==h and sources[ch][a:b]==q
        for ch,h,a,b,q in evidence)
    report['activation_eligible'] = (r['prompt_version']==PROMPT_VERSION and qualified(r['evaluation']) and all(checks) and len(jobs)>0
                                   and report['evidence_valid'] and all(j[1]=='done' for j in jobs) and all(c[4] for c in claims))
    report['review_hash'] = digest(report)
    await db.execute('UPDATE graph_revision SET review=%s WHERE id=%s AND version=%s',
                     (Jsonb(report),rid,r['version']))
    return report


async def record_review(db,cfg,rid,document):
    """Record explicit operator assessments, not caller-supplied aggregate scores."""
    report=await preview(db,cfg,rid)
    if document.get('review_hash')!=report['review_hash'] or not document.get('reviewer') or document.get('approved') is not True:
        raise ValueError('review must explicitly approve the current report hash and name its reviewer')
    mentions=document.get('mentions',[]);facts=document.get('facts',[])
    actual={str(mid):bool(linked) for mid,linked in await(await db.execute('''SELECT m.id,EXISTS(SELECT 1 FROM mention_binding b
        WHERE b.revision_id=m.revision_id AND b.mention_id=m.id) FROM source_mention m WHERE revision_id=%s''',(rid,))).fetchall()}
    mids=[m['id'] for m in mentions];fids=[f['id'] for f in facts]
    if len(set(mids))!=len(mids) or not set(mids)<=set(actual):
        raise ValueError('review contains duplicate or unknown mention IDs')
    if len(set(fids))!=len(fids) or set(fids)!={c['id'] for c in report['claims']}:
        raise ValueError('review must assess every published fact exactly once')
    if any(type(x.get('correct')) is not bool for x in mentions+facts) or any(type(x.get('unambiguous')) is not bool for x in mentions):
        raise ValueError('review assessments must contain explicit boolean judgments')
    linked=[m for m in mentions if actual[m['id']]]
    unambiguous=[m for m in mentions if m['unambiguous']]
    metrics=dict(reviewed=True,publication_review_complete=True,reviewed_mentions=len(mentions),reviewed_facts=len(facts),
        link_precision=sum(m['correct'] for m in linked)/len(linked) if linked else 0,
        unambiguous_recall=sum(m['correct'] and actual[m['id']] for m in unambiguous)/len(unambiguous) if unambiguous else 0,
        fact_precision=sum(f['correct'] for f in facts)/len(facts) if facts else 0,
        merge_regressions=document.get('known_merge_regressions',1),evidence_valid=all(c['quote'] for c in report['claims']))
    async with db.transaction():
        r=await revision(db,rid,lock=True)
        if r['version']!=report['version'] or r['state']!='staging':
            raise ValueError('revision changed while reviewing')
        await db.execute('UPDATE graph_revision SET evaluation=%s,review=NULL WHERE id=%s',(Jsonb(metrics),rid))
        await db.execute('INSERT INTO graph_audit(novel_id,revision_id,action,detail) VALUES(%s,%s,%s,%s)',
            (r['novel_id'],rid,'review',Jsonb(document)))
    return await preview(db,cfg,rid)


async def switch(db,cfg,rid,review_hash=None, *, rollback=False):
    # Object integrity is rechecked before acquiring the short cutover lock.
    report = None if rollback else await preview(db,cfg,rid)
    if not rollback and await local_model(cfg,report['model']['name'])!=report['model']:
        raise ValueError('model changed after review')
    async with db.transaction():
        r = await revision(db,rid)
        old = (await (await db.execute('SELECT active_graph_revision FROM novel WHERE id=%s FOR UPDATE',(r['novel_id'],))).fetchone())[0]
        # Deterministic lock order avoids cutover/cutover deadlocks.
        await db.execute('SELECT id FROM graph_revision WHERE id=ANY(%s::uuid[]) ORDER BY id FOR UPDATE',([str(old),rid],))
        r = await revision(db,rid)
        if not rollback:
            if (not report['activation_eligible'] or report['review_hash']!=review_hash
                or r['version']!=report['version'] or r['generation']!=report['generation']
                or r['evaluation']!=report['evaluation'] or r['review']!=report):
                raise ValueError('activation requires the current qualifying report and explicit matching review hash')
            if r['state']!='staging':
                raise ValueError('only a staging revision can be activated')
        elif r['state']!='archived':
            raise ValueError('rollback target must be an archived revision')
        await db.execute("UPDATE graph_revision SET state='archived',generation=generation+1,version=version+1 WHERE id=%s",(old,))
        await db.execute("UPDATE graph_revision SET state='active',trusted=CASE WHEN %s THEN trusted ELSE true END,generation=generation+1,version=version+1 WHERE id=%s",(rollback,rid))
        await db.execute('UPDATE novel SET active_graph_revision=%s WHERE id=%s',(rid,r['novel_id']))
        await db.execute('INSERT INTO graph_audit(novel_id,revision_id,action,detail) VALUES(%s,%s,%s,%s)',
                         (r['novel_id'],rid,'rollback' if rollback else 'activate',Jsonb(dict(previous=str(old),review_hash=review_hash))))
    if not rollback:
        await enqueue_completed(db,cfg,r['novel_id'])


async def enqueue_completed(db,cfg,novel):
    row=await (await db.execute("SELECT r.id FROM graph_revision r JOIN novel n ON n.active_graph_revision=r.id WHERE n.id=%s AND r.trusted AND NOT r.legacy",(novel,))).fetchone()
    if not row:
        return
    r=await revision(db,str(row[0]))
    client=objects(cfg)
    known={c['chapter'] for c in r['snapshot']['chapters']}
    rows=await (await db.execute("SELECT chapter_index,raw_uri,translated_uri,raw_hash FROM chapter WHERE novel_id=%s AND translation_ready ORDER BY chapter_index",(novel,))).fetchall()
    additions=[]
    for index,raw_uri,translated_uri,raw_hash in rows:
        if index in known: continue
        source=await asyncio.to_thread(read_object,client,cfg,raw_uri)
        display=await asyncio.to_thread(read_object,client,cfg,translated_uri or raw_uri)
        additions.append(dict(chapter=index,raw_uri=raw_uri,translated_uri=translated_uri,raw_hash=raw_hash,
                              source_hash=digest(source),display_hash=digest(display)))
    if not additions: return
    async with db.transaction():
        current=await revision(db,r['id'],lock=True)
        if current['generation']!=r['generation'] or current['state']!='active': return
        known={c['chapter'] for c in current['snapshot']['chapters']}
        for c in additions:
            if c['chapter'] in known: continue
            current['snapshot']['chapters'].append(c)
            await db.execute('''INSERT INTO graph_job(revision_id,chapter_index,input_hash,model_identity,generation)
                VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
                (r['id'],c['chapter'],digest(c),digest(r['model']),r['generation']))
        await db.execute('UPDATE graph_revision SET snapshot=%s,version=version+1 WHERE id=%s',(Jsonb(current['snapshot']),r['id']))


async def next_retryable_active_revision(db):
    # Only the earliest unfinished chapter may advance identity state. A retryable
    # failure wakes at its scheduled time; a terminal failure continues to fence all
    # later chapters until an operator explicitly resumes it.
    row=await (await db.execute("""SELECT r.id FROM graph_revision r
            JOIN LATERAL (SELECT state,attempts,retry_at FROM graph_job
                          WHERE revision_id=r.id AND state<>'done'
                          ORDER BY chapter_index LIMIT 1) j ON true
            WHERE r.state='active' AND r.trusted AND NOT r.legacy AND
              (j.state IN ('pending','processing') OR
               (j.state='failed' AND j.attempts<=3 AND j.retry_at<=now()))
            ORDER BY r.created_at LIMIT 1""")).fetchone()
    return str(row[0]) if row else None


async def drain_active(cfg):
    async with await psycopg.AsyncConnection.connect(cfg.database_url,autocommit=True) as db:
        rid=await next_retryable_active_revision(db)
        if rid:
            await resume(db,cfg,rid,limit=1)


async def main(args):
    cfg = Config.load()
    if args.command=='select-model':
        selected=select_model([json.loads(Path(p).read_text()) for p in args.reports])
        print(json.dumps(dict(selected=selected,status='qualified' if selected else 'No qualifying local model; keep quarantine.')))
        return
    async with await psycopg.AsyncConnection.connect(cfg.database_url,autocommit=True) as db:
        if args.command=='prepare':
            result = dict(revision=await prepare(db,cfg,args.novel,args.model),status='quarantined; awaiting evaluation and rebuild')
        elif args.command=='resume':
            await resume(db,cfg,args.revision,limit=args.limit)
            result = dict(status='resume finished')
        elif args.command=='preview':
            result = await preview(db,cfg,args.revision)
        elif args.command=='review':
            result = await record_review(db,cfg,args.revision,json.loads(Path(args.file).read_text()))
        else:
            await switch(db,cfg,args.revision,getattr(args,'review_hash',None),rollback=args.command=='rollback')
            result = dict(status=args.command,revision=args.revision)
        rendered = json.dumps(result,ensure_ascii=False,indent=2,default=str)
        if getattr(args,'output',None):
            Path(args.output).write_text(rendered+'\n')
        print(rendered)


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command',required=True)
    p=commands.add_parser('select-model');p.add_argument('--reports',nargs='+',required=True)
    p = commands.add_parser('prepare'); p.add_argument('--novel',required=True); p.add_argument('--model',required=True)
    for name in ['resume','preview','review','activate','rollback']:
        p = commands.add_parser(name); p.add_argument('--revision',required=True)
        if name=='resume': p.add_argument('--limit',type=int)
        if name=='preview': p.add_argument('--output')
        if name=='review': p.add_argument('--file',required=True)
        if name=='activate': p.add_argument('--review-hash',required=True)
    asyncio.run(main(parser.parse_args()))
