"""Review-gated local graph repair. Never rewrites source, translations or glossary.

python -m pipeline.graph_rebuild prepare --novel UUID --model llama3.2:3b
python -m pipeline.graph_rebuild extend --novel UUID --upto 10
python -m pipeline.graph_rebuild resume --revision UUID
python -m pipeline.graph_rebuild preview --revision UUID --output report.json
python -m pipeline.graph_rebuild activate --revision UUID --review-hash SHA256
python -m pipeline.graph_rebuild rollback --revision UUID

Activation is an explicit operator action and requires a frozen qualifying report.
Bounded revisions grow only through their recorded chapter ceiling (§0.2).
"""
from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import replace
from pathlib import Path

import httpx
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from pipeline.config import Config
from pipeline.context import PipelineState
from pipeline.envelope import ChapterEnvelope, SourceMeta
from pipeline.evidence import PROMPT_VERSION, digest
from pipeline.failures import CANCELLED, clear_blocked, failure_category, record_blocked
from pipeline.knowledge import KnowledgeEngine
from pipeline.llm.provider import AdmissionRejected
from pipeline.provider_config import (
    ProviderConfigRow, build_provider, load_provider_config, load_provider_credential,
    resolve_provider_config,
)
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
    total_mentions=metrics.get('total_mentions',metrics.get('reviewed_mentions',0))
    total_facts=metrics.get('total_facts',metrics.get('reviewed_facts',0))
    return (total_mentions>0 and total_facts>0
            and ('total_mentions' not in metrics or metrics.get('reviewed_mentions')==total_mentions)
            and ('total_facts' not in metrics or metrics.get('reviewed_facts')==total_facts)
            and metrics.get('reviewed_mentions',0)>=min(60,total_mentions)
            and metrics.get('reviewed_facts',0)>=min(30,total_facts)
            and (metrics.get('link_precision') or 0)>=.98 and metrics.get('unambiguous_recall',0)>=.90
            and (metrics.get('fact_precision') or 0)>=.95 and metrics.get('merge_regressions',1)==0
            and metrics.get('evidence_valid',False) and metrics.get('reviewed',False)
            and metrics.get('publication_review_complete',False))


def select_model(reports):
    candidates=[]
    for report in reports:
        m=report['metrics']
        if (report['model']['provider']=='ollama' and not m.get('failures')
            and m.get('tested_mentions',0)>=60 and m.get('tested_facts',0)>=30
            and m.get('candidate_recall')==1
            and m.get('model_inference_seconds') is not None
            and qualified(dict(m,publication_review_complete=True))):
            candidates.append(report)
    if not candidates:
        return None
    return min(candidates,key=lambda r:(-r['metrics']['link_precision'],-r['metrics']['fact_precision'],r['metrics']['model_inference_seconds']))['model']


def _flatten(value: dict, prefix: str = "") -> dict:
    flat = {}
    for key, item in value.items():
        path = f"{prefix}{key}"
        if isinstance(item, dict):
            flat.update(_flatten(item, f"{path}."))
        else:
            flat[path] = item
    return flat


def model_drift(pinned: dict, live: dict) -> str:
    """Name the fields that differ between a revision's pinned model and the live one.

    The callers test whole-dict inequality, which is right, but reporting that as "the
    model changed" sends a reader to inspect the model when the mismatch is usually a
    runtime setting. A `think` flag set on `prepare` and forgotten on `resume` is
    indistinguishable from swapped weights in the message, and costs a debugging session
    to tell apart. Nested identity is flattened so the differing key is named directly.
    """
    a, b = _flatten(pinned), _flatten(live)
    parts = [
        f"{key}: pinned {a.get(key)!r}, live {b.get(key)!r}"
        for key in sorted(set(a) | set(b))
        if a.get(key) != b.get(key)
    ]
    return "; ".join(parts) or "no field differs"


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
    # Only generation-affecting settings identify a graph revision. Deadline budgets are
    # operational controls and stay live in Config for resumable work. num_ctx is
    # deliberately absent here too -- it is discovered per host by discover_num_ctx(),
    # called only from prepare(), and is excluded from drift comparisons by
    # _without_num_ctx() below. This function must stay side-effect-free: preflight
    # calls it and promises never to load a model or generate a token.
    return dict(provider='ollama',name=name,digest=matches[0]['digest'],
                identity=graph_runtime(cfg)['identity'])


HOSTED_GRAPH_PROVIDERS = frozenset({'anthropic','deepseek','gemini'})


async def graph_provider_config(db, cfg, novel: str, provider: str, model: str
                                ) -> ProviderConfigRow:
    """Resolve a graph-only provider without changing the book's translation routing.

    A matching per-book key wins. Otherwise the account credential for the requested
    provider is used. This lets graph extraction use an API key independently of the
    provider that produced the saved translation (§5.4).
    """
    book=await load_provider_config(db,novel)
    if book is not None and book.provider==provider:
        effective=await resolve_provider_config(db,novel,provider)
        assert effective is not None
    else:
        base_url,api_key=await load_provider_credential(db,provider)
        effective=ProviderConfigRow(provider=provider,model=None,translate_model=None,
            extract_model=None,base_url=base_url,api_key=api_key)
    return replace(effective,extract_model=model)


async def graph_model_identity(db, cfg, novel: str, provider: str, model: str) -> dict:
    if provider=='ollama':
        return await local_model(cfg,model)
    if provider not in HOSTED_GRAPH_PROVIDERS:
        raise ValueError('graph provider must be anthropic, deepseek, gemini, or ollama')
    # Constructing validates that required credentials exist without spending a model call.
    configured=await graph_provider_config(db,cfg,novel,provider,model)
    candidate=build_provider(configured,cfg)
    await candidate.aclose()
    return dict(provider=provider,name=model,strategy='api_two_pass')


async def graph_completion_provider(db, cfg, revision_row: dict):
    model=revision_row['model']
    if model['provider']=='ollama':
        return None
    configured=await graph_provider_config(
        db,cfg,revision_row['novel_id'],model['provider'],model['name'])
    return build_provider(configured,cfg)


async def discover_num_ctx(cfg, name):
    """Ask for cfg.graph_ollama_num_ctx_target, then read back what Ollama actually
    loaded the model with (§ dynamic context sizing).

    Not a bare request-and-hope: Ollama's own vram-based DEFAULT optimizes for keeping
    several models resident on a shared card at once, not for this workload's own
    prompts -- on the same 8 GB RTX 2070 that ran real extraction calls needing 6-10k
    input tokens alone, that default came out to 4096. Asking for the target explicitly
    makes Ollama evict competing idle models to fit it rather than silently handing back
    a window too small to be useful; a fixed 16384 chosen once still starved a smaller
    card elsewhere, which is why this reads back the actual value instead of trusting the
    ask -- a host that genuinely cannot fit the target fails loudly here, at prepare()
    time, rather than mid-chapter with a truncated response or an OOM-killed server.

    POST /api/generate with no prompt loads the model without generating a token, per
    Ollama's own API contract; this is the one place in the codebase allowed to do that
    (call only from prepare(), never from local_model() or preflight -- see their notes).
    """
    async with httpx.AsyncClient(base_url=cfg.ollama_host,
                                  timeout=cfg.graph_ollama_first_token_seconds or 120) as client:
        probe = await client.post('/api/generate', json=dict(
            model=name, options=dict(num_ctx=cfg.graph_ollama_num_ctx_target)))
        probe.raise_for_status()
        loaded = await client.get('/api/ps')
        loaded.raise_for_status()
    resident = next((m for m in loaded.json()['models'] if m['name']==name), None)
    if not resident or not resident.get('context_length'):
        raise ValueError(f'{name} did not report a resident context length after loading')
    return resident['context_length']


def _without_num_ctx(model: dict) -> dict:
    """Strip num_ctx from a model dict before comparing it for drift.

    num_ctx is host capacity, not a generation-affecting choice (§ dynamic context
    sizing): the same weights answer identically whether Ollama sized their window at
    8192 or 16384, as long as a call actually fits. Comparing it as drift would force a
    fresh revision every time the configured Ollama host's available memory shifted for
    reasons that have nothing to do with reproducibility -- exactly the kind of
    operational fact `identity`'s own docstring says does not belong there.
    """
    identity = model.get('identity')
    if not identity or 'num_ctx' not in identity:
        return model
    return dict(model, identity={k: v for k, v in identity.items() if k != 'num_ctx'})


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


def _chapter_ceiling(value):
    if value is None:
        return None
    if type(value) is not int or not 0 <= value <= 2_147_483_647:
        raise ValueError('upto_chapter must be a nonnegative integer')
    return value


def _ready_prefix(rows, *, start):
    """Return the contiguous ready prefix; identity state may never cross a gap (§0.2)."""
    prefix=[]
    expected=start
    for row in rows:
        index,*chapter,ready=row
        if index<expected:
            continue
        if index!=expected or not ready:
            break
        prefix.append((index,*chapter))
        expected+=1
    return prefix,expected


async def prepare(db,cfg,novel,model, *, upto_chapter=None, provider='ollama'):
    upto_chapter=_chapter_ceiling(upto_chapter)
    identity = await graph_model_identity(db,cfg,novel,provider,model)
    if provider=='ollama':
        # Pin num_ctx to whatever this host actually loaded the model with (§ dynamic
        # context sizing), not a value chosen ahead of time -- see discover_num_ctx().
        num_ctx = await discover_num_ctx(cfg,model)
        if num_ctx <= identity['identity']['num_predict']:
            raise ValueError(f'{model} loaded with a {num_ctx}-token context on this host, which leaves '
                              f'no room for the configured {identity["identity"]["num_predict"]}-token output '
                              'budget; reduce GRAPH_OLLAMA_NUM_PREDICT or free VRAM on this host')
        identity = dict(identity, identity=dict(identity['identity'], num_ctx=num_ctx))
    client = objects(cfg)
    row = await (await db.execute('SELECT ontology FROM novel WHERE id=%s',(novel,))).fetchone()
    if not row:
        raise ValueError('novel not found')
    ontology = row[0]
    ontology['kinds'] = list(dict.fromkeys(ontology['kinds']+['place','group']))
    ontology['attributes'] = [a for a in ontology['attributes'] if a['name']!='description']+[
        dict(name='description',kinds=ontology['kinds'])]
    # Snapshot only durably completed chapters; do not manipulate ordinary jobs.
    if upto_chapter is None:
        rows = await (await db.execute('''SELECT chapter_index,raw_uri,translated_uri,raw_hash FROM chapter
            WHERE novel_id=%s AND status='done' ORDER BY chapter_index''',(novel,))).fetchall()
        start_chapter=rows[0][0] if rows else 1
    else:
        candidates = await (await db.execute('''SELECT chapter_index,raw_uri,translated_uri,raw_hash,status='done'
            FROM chapter WHERE novel_id=%s AND chapter_index<=%s ORDER BY chapter_index''',
            (novel,upto_chapter))).fetchall()
        start_chapter=0 if candidates and candidates[0][0]==0 else 1
        rows,_=_ready_prefix(candidates,start=start_chapter)
        if not rows:
            raise ValueError('no contiguous completed chapters are available through upto_chapter')
    chapters = []
    for index,raw_uri,translated_uri,raw_hash in rows:
        source = await asyncio.to_thread(read_object,client,cfg,raw_uri)
        display = await asyncio.to_thread(read_object,client,cfg,translated_uri or raw_uri)
        chapters.append(dict(chapter=index,raw_uri=raw_uri,translated_uri=translated_uri,
                             raw_hash=raw_hash,source_hash=digest(source),display_hash=digest(display)))
    glossary = await (await db.execute('SELECT row_to_json(g) FROM glossary g WHERE novel_id=%s ORDER BY source_term',(novel,))).fetchall()
    progress = await (await db.execute('SELECT row_to_json(p) FROM reader_progress p WHERE novel_id=%s ORDER BY reader_id',(novel,))).fetchall()
    snapshot = dict(chapters=chapters,glossary=[g[0] for g in glossary],progress=[p[0] for p in progress])
    if upto_chapter is not None:
        snapshot.update(upto_chapter=upto_chapter,start_chapter=start_chapter)
    async with db.transaction():
        old = await (await db.execute('SELECT active_graph_revision FROM novel WHERE id=%s FOR UPDATE',(novel,))).fetchone()
        if not old:
            raise ValueError('novel not found')
        # Lock waits for a publication transaction to finish; future chapters cannot write.
        if old[0] is not None:
            await db.execute('UPDATE graph_revision SET trusted=false,generation=generation+1,version=version+1 WHERE id=%s',(old[0],))
        rid = (await (await db.execute('''INSERT INTO graph_revision(novel_id,ontology,model,snapshot,prompt_version)
            VALUES(%s,%s,%s,%s,%s) RETURNING id''',(novel,Jsonb(ontology),Jsonb(identity),Jsonb(snapshot),PROMPT_VERSION))).fetchone())[0]
        if old[0] is not None:
            await db.execute('INSERT INTO graph_audit(novel_id,revision_id,action,detail) VALUES(%s,%s,%s,%s)',
                             (novel,old[0],'quarantine',Jsonb(dict(replacement=str(rid)))))
        else:
            # A reader may explicitly delete every revision and later start over. The
            # new revision is still staged and untrusted; this audit only records origin.
            await db.execute('INSERT INTO graph_audit(novel_id,revision_id,action,detail) VALUES(%s,%s,%s,%s)',
                             (novel,rid,'prepare',Jsonb(dict(from_empty=True))))
        for c in chapters:
            await db.execute('''INSERT INTO graph_job(revision_id,chapter_index,input_hash,model_identity,generation)
                VALUES(%s,%s,%s,%s,1)''',(rid,c['chapter'],digest(c),digest(identity)))
    return str(rid)


def graph_retry_delay_minutes(attempt: int) -> int | None:
    return {1:5,2:15,3:45}.get(attempt)


async def record_job_failure(db,rid,index,exc):
    """Mark one chapter failed with a safe class and its next retry time.

    Its own function because this path, by definition, only runs when something has
    already gone wrong -- the worst place for an untested SQL statement. Inline in the
    except block it was reachable only by driving a real model to failure; here a test can
    call it against real rows. Only ``category`` is ever returned to a reader; ``error``
    stays behind the database for operator debugging (migration 0046).
    """
    attempts=(await(await db.execute('SELECT attempts FROM graph_job WHERE revision_id=%s AND chapter_index=%s',(rid,index))).fetchone())[0]
    delay=graph_retry_delay_minutes(attempts)
    await db.execute("""UPDATE graph_job SET state='failed',error=%s,category=%s,
        retry_at=CASE WHEN %s::int IS NULL THEN NULL ELSE now()+(%s::int*interval '1 minute') END,
        updated_at=now() WHERE revision_id=%s AND chapter_index=%s""",
                     ((type(exc).__name__+': '+str(exc))[:2000],failure_category(exc),delay,delay,rid,index))
    return delay


async def record_job_interruption(db,rid,index):
    """Make an interrupted chapter visible and immediately resumable.

    attempts is incremented before a chapter is attempted (this run's own UPDATE, above),
    so an interruption -- the worker being restarted or stopped, never anything wrong
    with the chapter -- would otherwise spend one of the three genuine-failure attempts a
    real content or model error gets. A worker restarted three times in a row while the
    SAME chapter happened to be mid-attempt each time silently exhausted its budget and
    permanently stranded the revision with no failed chapter to point at -- refund the
    attempt here so only real failures count against it.
    """
    await db.execute("""UPDATE graph_job SET state='failed',
        error='CancelledError: extraction interrupted',category=%s,
        attempts=greatest(attempts-1,0),retry_at=now(),updated_at=now()
        WHERE revision_id=%s AND chapter_index=%s""",(CANCELLED,rid,index))


async def resume(db,cfg,rid, *, limit=None):
    # Session advisory lock means interrupted jobs can be retried immediately, while
    # two resume processes cannot independently advance a revision out of order.
    locked = (await (await db.execute('SELECT pg_try_advisory_lock(hashtextextended(%s,0))',(rid,))).fetchone())[0]
    if not locked:
        raise RuntimeError('another worker is already enriching this revision')
    engine = None
    try:
        # Everything up to the chapter loop is the preamble: it can fail for reasons that
        # belong to the run, not to any chapter, and used to die invisibly.
        try:
            r = await revision(db,rid)
            if r['state']=='archived' or r['legacy']:
                raise ValueError('revision cannot be rebuilt')
            live = await graph_model_identity(
                db,cfg,r['novel_id'],r['model']['provider'],r['model']['name'])
            if _without_num_ctx(live) != _without_num_ctx(r['model']):
                raise ValueError('installed model or inference configuration changed since snapshot; '
                                 f'create a new revision ({model_drift(r["model"], live)})')
            provider=await graph_completion_provider(db,cfg,r)
            engine = KnowledgeEngine(db,cfg,r,provider=provider)
            client = objects(cfg)
            lang = await (await db.execute('SELECT source_lang,target_lang FROM novel WHERE id=%s',(r['novel_id'],))).fetchone()
            jobs = await (await db.execute('SELECT chapter_index FROM graph_job WHERE revision_id=%s AND state<>%s ORDER BY chapter_index',
                                          (rid,'done'))).fetchall()
        except Exception as exc:
            await record_blocked(db,'graph_revision',rid,exc)
            raise
        await clear_blocked(db,'graph_revision',rid)
        for (index,) in jobs[:limit] if limit else jobs:
            c = next(c for c in r['snapshot']['chapters'] if c['chapter']==index)
            source = await asyncio.to_thread(read_object,client,cfg,c['raw_uri'])
            display = await asyncio.to_thread(read_object,client,cfg,c['translated_uri'] or c['raw_uri'])
            if digest(source)!=c['source_hash'] or digest(display)!=c['display_hash']:
                raise ValueError('saved prose changed since snapshot')
            await db.execute("UPDATE graph_job SET state='processing',attempts=attempts+1,error=NULL,category=NULL,retry_at=NULL,generation=%s,updated_at=now() WHERE revision_id=%s AND chapter_index=%s",(r['generation'],rid,index))
            print(json.dumps(dict(revision=rid,chapter=index,state='processing')),flush=True)
            try:
                output = await engine.extract(r['novel_id'],index,source,display,lang[1])
                live = await graph_model_identity(
                    db,cfg,r['novel_id'],r['model']['provider'],r['model']['name'])
                if _without_num_ctx(live) != _without_num_ctx(r['model']):
                    raise ValueError('model or inference configuration changed during extraction; '
                                     f'publication refused ({model_drift(r["model"], live)})')
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
                if engine.run_id:
                    await db.execute("UPDATE chapter_knowledge_run SET state='pending',updated_at=now() WHERE id=%s",(engine.run_id,))
                await db.execute("UPDATE graph_job SET state='pending',error=NULL,category=NULL,retry_at=NULL,updated_at=now() WHERE revision_id=%s AND chapter_index=%s",(rid,index))
                raise
            except asyncio.CancelledError:
                # SIGINT/SIGTERM must not leave a dead process looking like active work.
                # The completed graph_completion rows remain reusable; this chapter can
                # resume immediately from its first incomplete request (§0, §5.4).
                if engine.run_id:
                    await engine._activity('run','run','rejected',{'reason':'extraction interrupted'})
                    await db.execute("UPDATE chapter_knowledge_run SET state='failed',error='extraction interrupted',updated_at=now() WHERE id=%s",(engine.run_id,))
                await record_job_interruption(db,rid,index)
                raise
            except Exception as exc:
                if engine.run_id:
                    await engine._activity('run','run','rejected',{'reason':str(exc)[:500]})
                    await db.execute("UPDATE chapter_knowledge_run SET state='failed',error=%s,updated_at=now() WHERE id=%s",(str(exc)[:2000],engine.run_id))
                await record_job_failure(db,rid,index,exc)
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


def translated_context(source: str, display: str, start: int, end: int, *, limit: int = 320) -> str:
    """Map source evidence to readable context from the saved translation.

    Translation is instructed to preserve paragraph breaks. Matching the non-empty line
    ordinal therefore gives review deterministic target-language context without turning
    translated prose into evidence (§0). If line structure differs, the global character
    ratio is a bounded display fallback; the source quote remains the audit evidence.
    """
    def lines(text):
        result=[];offset=0
        for line in text.splitlines(keepends=True):
            content=line.rstrip('\r\n')
            if content.strip():
                result.append((offset,offset+len(content),content))
            offset+=len(line)
        return result or ([(0,len(text),text)] if text.strip() else [])

    source_lines,display_lines=lines(source),lines(display)
    source_index=next((i for i,(lo,hi,_) in enumerate(source_lines)
                       if lo<=start<max(lo+1,hi)),None)
    if source_index is not None and len(source_lines)==len(display_lines):
        slo,shi,_=source_lines[source_index]
        _,_,target=display_lines[source_index]
        ratio=(((start+end)/2)-slo)/max(1,shi-slo)
        center=int(len(target)*max(0,min(1,ratio)))
    else:
        target=display
        center=int(len(display)*max(0,min(1,((start+end)/2)/max(1,len(source)))))
    if len(target)<=limit:
        return target.strip()
    lo=max(0,min(len(target)-limit,center-limit//2));hi=min(len(target),lo+limit)
    if lo:
        space=target.find(' ',lo,min(hi,lo+60))
        if space!=-1: lo=space+1
    if hi<len(target):
        space=target.rfind(' ',max(lo,hi-60),hi)
        if space!=-1: hi=space
    return target[lo:hi].strip()


async def preview(db,cfg,rid):
    r = await revision(db,rid)
    client = objects(cfg)
    checks = []
    sources={};displays={}
    for c in r['snapshot'].get('chapters',[]):
        source = await asyncio.to_thread(read_object,client,cfg,c['raw_uri'])
        sources[c['chapter']]=source
        display = await asyncio.to_thread(read_object,client,cfg,c['translated_uri'] or c['raw_uri'])
        displays[c['chapter']]=display
        saved=await (await db.execute('SELECT raw_uri,translated_uri,raw_hash FROM chapter WHERE novel_id=%s AND chapter_index=%s',
            (r['novel_id'],c['chapter']))).fetchone()
        checks.append(digest(source)==c['source_hash'] and digest(display)==c['display_hash']
                      and saved==(c['raw_uri'],c['translated_uri'],c['raw_hash']))
    jobs = await (await db.execute('SELECT chapter_index,state,error,output FROM graph_job WHERE revision_id=%s ORDER BY chapter_index',(rid,))).fetchall()
    claims = await (await db.execute('''SELECT coalesce(e.canonical_en,CASE WHEN e.kind='character' OR n.source_lang=n.target_lang THEN e.canonical END),f.attribute,coalesce(f.value_en,f.value),f.source_chapter,v.quote,v.source_hash,v.char_start,v.char_end,f.id,e.canonical
        FROM fact f JOIN entity e ON e.id=f.entity_id JOIN novel n ON n.id=f.novel_id LEFT JOIN graph_evidence v ON v.id=f.evidence_id
        WHERE f.revision_id=%s ORDER BY f.source_chapter,f.id''',(rid,))).fetchall()
    mention_rows=await(await db.execute('''SELECT m.id::text,m.chapter_index,m.surface,m.kind,
            coalesce(e.canonical_en,CASE WHEN e.kind='character' OR n.source_lang=n.target_lang THEN e.canonical END),
            v.quote,v.char_start,v.char_end,e.canonical,
            coalesce(dm.phrase,m.surface_en,e.canonical_en,
                     CASE WHEN e.kind='character' THEN e.canonical END,
                     CASE WHEN n.source_lang=n.target_lang THEN m.surface END)
        FROM source_mention m
        JOIN novel n ON n.id=m.novel_id
        LEFT JOIN LATERAL (SELECT entity_id FROM mention_binding b
            WHERE b.revision_id=m.revision_id AND b.mention_id=m.id
            ORDER BY known_from_chapter DESC LIMIT 1) b ON true
        LEFT JOIN entity e ON e.revision_id=m.revision_id AND e.id=b.entity_id
        LEFT JOIN LATERAL (SELECT phrase FROM display_mention d
            WHERE d.revision_id=m.revision_id AND d.mention_id=m.id
            ORDER BY d.char_start LIMIT 1) dm ON true
        LEFT JOIN graph_evidence v ON v.id=m.evidence_id
        WHERE m.revision_id=%s ORDER BY m.chapter_index,m.id''',(rid,))).fetchall()
    coverage = (await (await db.execute('''SELECT count(*),count(*) FILTER(WHERE EXISTS(SELECT 1 FROM mention_binding b
        WHERE b.revision_id=m.revision_id AND b.mention_id=m.id)) FROM source_mention m WHERE revision_id=%s''',(rid,))).fetchone())
    report = dict(revision=rid,generation=r['generation'],version=r['version'],model=r['model'],prompt_version=r['prompt_version'],
        ontology=r['ontology'],evaluation=r['evaluation'],saved_prose_unchanged=all(checks),
        completed=sum(j[1]=='done' for j in jobs),total_jobs=len(jobs),
        upto_chapter=r['snapshot'].get('upto_chapter'),
        mention_coverage=dict(total=coverage[0],linked=coverage[1],unresolved=coverage[0]-coverage[1]),
        mentions=[dict(id=m[0],chapter=m[1],surface=m[2],kind=m[3],entity=m[4],quote=m[5],
                       target_context=translated_context(sources[m[1]],displays[m[1]],m[6],m[7]),
                       entity_source=m[8],surface_target=m[9])
                  for m in mention_rows],
        claims=[dict(**dict(zip(['entity','attribute','value','chapter','quote','source_hash','start','end','id','entity_source'],c)),
                     target_context=translated_context(sources[c[3]],displays[c[3]],c[6],c[7]))
                for c in claims],
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
    if len(set(mids))!=len(mids) or set(mids)!=set(actual):
        raise ValueError('review must assess every source mention exactly once')
    if len(set(fids))!=len(fids) or set(fids)!={c['id'] for c in report['claims']}:
        raise ValueError('review must assess every published fact exactly once')
    if any(type(x.get('correct')) is not bool for x in mentions+facts) or any(type(x.get('unambiguous')) is not bool for x in mentions):
        raise ValueError('review assessments must contain explicit boolean judgments')
    linked=[m for m in mentions if actual[m['id']]]
    unambiguous=[m for m in mentions if m['unambiguous']]
    metrics=dict(reviewed=True,publication_review_complete=True,
        total_mentions=len(actual),total_facts=len(report['claims']),
        reviewed_mentions=len(mentions),reviewed_facts=len(facts),
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
    if not rollback and _without_num_ctx(await local_model(cfg,report['model']['name'])) != _without_num_ctx(report['model']):
        raise ValueError('model changed after review')
    async with db.transaction():
        r = await revision(db,rid)
        old = (await (await db.execute('SELECT active_graph_revision FROM novel WHERE id=%s FOR UPDATE',(r['novel_id'],))).fetchone())[0]
        # Deterministic lock order avoids cutover/cutover deadlocks.
        revision_ids=[rid] if old is None else [str(old),rid]
        await db.execute('SELECT id FROM graph_revision WHERE id=ANY(%s::uuid[]) ORDER BY id FOR UPDATE',(revision_ids,))
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
        if old is not None:
            await db.execute("UPDATE graph_revision SET state='archived',generation=generation+1,version=version+1 WHERE id=%s",(old,))
        await db.execute("UPDATE graph_revision SET state='active',trusted=CASE WHEN %s THEN trusted ELSE true END,generation=generation+1,version=version+1 WHERE id=%s",(rollback,rid))
        await db.execute('UPDATE novel SET active_graph_revision=%s WHERE id=%s',(rid,r['novel_id']))
        await db.execute('INSERT INTO graph_audit(novel_id,revision_id,action,detail) VALUES(%s,%s,%s,%s)',
                         (r['novel_id'],rid,'rollback' if rollback else 'activate',Jsonb(dict(previous=str(old),review_hash=review_hash))))
    if not rollback:
        await enqueue_completed(db,cfg,r['novel_id'])


async def discard(db,cfg,rid):
    """Undo prepare()'s precautionary quarantine of the revision this one replaced.

    prepare() flips the OLD active revision to trusted=false the instant a rebuild
    starts -- a precaution, not a verdict on that revision's own facts. rollback()
    deliberately does NOT restore trust, because its target was distrusted for a reason
    (it was itself once an active revision someone rolled back from). Discard is the
    opposite case: the staging revision it targets never published anything, so there is
    nothing to distrust the OLD revision over. Undoing a precaution one has not yet acted
    on is a different operation from undoing a real decision, so it gets its own verb
    instead of overloading rollback.

    Restoring trust is conditional, not automatic (§0: trusted=false stays a human
    decision) -- only if the revision prepare quarantined is STILL the novel's active
    revision, identified by the audit trail prepare itself wrote, and nothing has
    quarantined it again since. Otherwise the discard still removes the abandoned staging
    revision, but says plainly that trust was not restored, the same honesty discipline
    as rollback's own note (repair.py's "trust is not restored by rollback").
    """
    async with db.transaction():
        # Lock order is prepare()'s and switch()'s, in that order: the novel row first,
        # then the revisions by id. Reversing either half deadlocks against a concurrent
        # cutover. The novel row lock is also what makes the quarantine audit read below
        # correct -- prepare() takes this same row lock before writing the audit row this
        # function reads, so holding it means no rebuild can start underneath us.
        r = await revision(db,rid)
        await db.execute('SELECT active_graph_revision FROM novel WHERE id=%s FOR UPDATE',(r['novel_id'],))
        quarantined = await (await db.execute(
            "SELECT revision_id FROM graph_audit WHERE novel_id=%s AND action='quarantine'"
            " AND detail->>'replacement'=%s ORDER BY id DESC LIMIT 1",
            (r['novel_id'],rid))).fetchone()
        target = str(quarantined[0]) if quarantined else None
        await db.execute('SELECT id FROM graph_revision WHERE id=ANY(%s::uuid[]) ORDER BY id FOR UPDATE',
                         ([target,rid] if target else [rid],))
        # Re-read under the locks: state is only trustworthy once nothing else can move it.
        r = await revision(db,rid)
        if r['state']!='staging':
            raise ValueError('only a staging revision can be discarded')
        restored = None
        if target:
            still_active = await (await db.execute(
                'SELECT active_graph_revision FROM novel WHERE id=%s',(r['novel_id'],))).fetchone()
            reharmed = await (await db.execute(
                "SELECT 1 FROM graph_audit WHERE revision_id=%s AND action='quarantine' AND id>"
                " (SELECT id FROM graph_audit WHERE novel_id=%s AND action='quarantine'"
                "  AND detail->>'replacement'=%s ORDER BY id DESC LIMIT 1)",
                (target,r['novel_id'],rid))).fetchone()
            if still_active and str(still_active[0])==target and not reharmed:
                await db.execute(
                    'UPDATE graph_revision SET trusted=true,generation=generation+1,version=version+1 WHERE id=%s',
                    (target,))
                restored = target
        await db.execute(
            "UPDATE graph_revision SET state='archived',generation=generation+1,version=version+1 WHERE id=%s",
            (rid,))
        # 0054's partial unique index (novel_id,chapter_index,scope) covers a live run per
        # scope; abandoning the staging revision without settling its runs would leave
        # that index blocking a fresh run against whatever revision replaces it next.
        await db.execute(
            "UPDATE chapter_knowledge_run SET state='rejected',updated_at=now()"
            " WHERE revision_id=%s AND state IN ('pending','processing','awaiting_review')",
            (rid,))
        await db.execute('INSERT INTO graph_audit(novel_id,revision_id,action,detail) VALUES(%s,%s,%s,%s)',
                         (r['novel_id'],rid,'discard',Jsonb(dict(restored=restored))))
    return dict(revision=rid,restored=restored)


async def enqueue_completed(db,cfg,novel, *, upto_chapter=None):
    upto_chapter=_chapter_ceiling(upto_chapter)
    row=await (await db.execute("SELECT r.id FROM graph_revision r JOIN novel n ON n.active_graph_revision=r.id WHERE n.id=%s AND r.trusted AND NOT r.legacy",(novel,))).fetchone()
    if not row:
        return dict(revision=None,added=[],upto_chapter=None,first_gap=None)
    r=await revision(db,str(row[0]))
    client=objects(cfg)
    known={c['chapter'] for c in r['snapshot']['chapters']}
    persisted=r['snapshot'].get('upto_chapter')
    ceiling=(persisted if upto_chapter is None else min(persisted,upto_chapter)
             if persisted is not None else upto_chapter)
    if ceiling is None:
        rows=await(await db.execute("SELECT chapter_index,raw_uri,translated_uri,raw_hash FROM chapter WHERE novel_id=%s AND translation_ready ORDER BY chapter_index",(novel,))).fetchall()
        first_gap=None
    else:
        all_rows=await(await db.execute('''SELECT chapter_index,raw_uri,translated_uri,raw_hash,translation_ready
            FROM chapter WHERE novel_id=%s AND chapter_index<=%s ORDER BY chapter_index''',
            (novel,ceiling))).fetchall()
        start=(max(known)+1) if known else r['snapshot'].get('start_chapter',0 if all_rows and all_rows[0][0]==0 else 1)
        rows,next_missing=_ready_prefix(all_rows,start=start)
        first_gap=next_missing if next_missing<=ceiling else None
    additions=[]
    for index,raw_uri,translated_uri,raw_hash in rows:
        if index in known: continue
        source=await asyncio.to_thread(read_object,client,cfg,raw_uri)
        display=await asyncio.to_thread(read_object,client,cfg,translated_uri or raw_uri)
        additions.append(dict(chapter=index,raw_uri=raw_uri,translated_uri=translated_uri,raw_hash=raw_hash,
                              source_hash=digest(source),display_hash=digest(display)))
    if not additions:
        return dict(revision=r['id'],added=[],upto_chapter=persisted,first_gap=first_gap)
    added=[]
    async with db.transaction():
        current=await revision(db,r['id'],lock=True)
        if current['generation']!=r['generation'] or current['state']!='active':
            return dict(revision=r['id'],added=[],upto_chapter=current['snapshot'].get('upto_chapter'),first_gap=first_gap)
        known={c['chapter'] for c in current['snapshot']['chapters']}
        for c in additions:
            if c['chapter'] in known: continue
            saved=await(await db.execute('''SELECT raw_uri,translated_uri,raw_hash,translation_ready FROM chapter
                WHERE novel_id=%s AND chapter_index=%s FOR SHARE''',(novel,c['chapter']))).fetchone()
            if not saved or saved!=(c['raw_uri'],c['translated_uri'],c['raw_hash'],True):
                first_gap=c['chapter']
                break
            current['snapshot']['chapters'].append(c)
            await db.execute('''INSERT INTO graph_job(revision_id,chapter_index,input_hash,model_identity,generation)
                VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
                (r['id'],c['chapter'],digest(c),digest(r['model']),r['generation']))
            added.append(c['chapter'])
        if added:
            await db.execute('UPDATE graph_revision SET snapshot=%s,version=version+1 WHERE id=%s',(Jsonb(current['snapshot']),r['id']))
    return dict(revision=r['id'],added=added,
                upto_chapter=current['snapshot'].get('upto_chapter'),first_gap=first_gap)


async def extend(db,cfg,novel, *, upto_chapter):
    """Raise a bounded active revision's ceiling, then append its next ready prefix."""
    upto_chapter=_chapter_ceiling(upto_chapter)
    if upto_chapter is None:
        raise ValueError('extend requires upto_chapter')
    async with db.transaction():
        row=await(await db.execute('SELECT active_graph_revision FROM novel WHERE id=%s FOR UPDATE',(novel,))).fetchone()
        if not row or row[0] is None:
            raise ValueError('active graph revision not found for extension')
        r=await revision(db,str(row[0]),lock=True)
        if r['state']!='active' or not r['trusted'] or r['legacy']:
            raise ValueError('active graph revision is not extendable')
        persisted=r['snapshot'].get('upto_chapter')
        if persisted is not None and upto_chapter>persisted:
            r['snapshot']['upto_chapter']=upto_chapter
            await db.execute('UPDATE graph_revision SET snapshot=%s,version=version+1 WHERE id=%s',
                             (Jsonb(r['snapshot']),r['id']))
        await db.execute('INSERT INTO graph_audit(novel_id,revision_id,action,detail) VALUES(%s,%s,%s,%s)',
                         (novel,r['id'],'extend',Jsonb(dict(upto_chapter=upto_chapter))))
    return await enqueue_completed(db,cfg,novel,upto_chapter=upto_chapter)


async def next_retryable_active_revision(db, novel_id=None, preferred_novel=None):
    # Only the earliest unfinished chapter may advance identity state. A retryable
    # failure wakes at its scheduled time; a terminal failure continues to fence all
    # later chapters until an operator explicitly resumes it.
    row=await (await db.execute("""SELECT r.id FROM graph_revision r
            JOIN LATERAL (SELECT state,attempts,retry_at FROM graph_job
                          WHERE revision_id=r.id AND state<>'done'
                          ORDER BY chapter_index LIMIT 1) j ON true
            WHERE (%s::uuid IS NULL OR r.novel_id=%s::uuid) AND
              r.state='active' AND r.trusted AND NOT r.legacy AND
              (j.state IN ('pending','processing') OR
               (j.state='failed' AND j.attempts<=3 AND j.retry_at<=now()))
            ORDER BY (r.novel_id=%s::uuid) DESC NULLS LAST, r.created_at LIMIT 1""",
            (novel_id, novel_id, preferred_novel))).fetchone()
    return str(row[0]) if row else None


async def drain_active(cfg, novel_id=None, preferred_novel=None):
    async with await psycopg.AsyncConnection.connect(cfg.database_url,autocommit=True) as db:
        rid=await next_retryable_active_revision(db, novel_id, preferred_novel)
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
            result = dict(revision=await prepare(db,cfg,args.novel,args.model,
                upto_chapter=args.upto,provider=args.provider),
                status='quarantined; awaiting evaluation and rebuild')
        elif args.command=='extend':
            result = await extend(db,cfg,args.novel,upto_chapter=args.upto)
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
    p = commands.add_parser('prepare'); p.add_argument('--novel',required=True); p.add_argument('--model',required=True); p.add_argument('--upto',type=int); p.add_argument('--provider',choices=['ollama','anthropic','deepseek','gemini'],default='ollama')
    p = commands.add_parser('extend'); p.add_argument('--novel',required=True); p.add_argument('--upto',type=int,required=True)
    for name in ['resume','preview','review','activate','rollback']:
        p = commands.add_parser(name); p.add_argument('--revision',required=True)
        if name=='resume': p.add_argument('--limit',type=int)
        if name=='preview': p.add_argument('--output')
        if name=='review': p.add_argument('--file',required=True)
        if name=='activate': p.add_argument('--review-hash',required=True)
    asyncio.run(main(parser.parse_args()))
