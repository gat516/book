"""Benchmark installed local models on the same reviewed, saved-book cases.

Evaluation revisions are isolated staging graphs, never reader selectable. Semantic
fact verification is scored separately from the still-required publication review.
"""
import argparse
import asyncio
from collections import Counter, defaultdict
import json
from pathlib import Path
import time

import psycopg
from psycopg.types.json import Jsonb

from pipeline.config import Config
from pipeline.evidence import Verification, digest, PROMPT_VERSION
from pipeline.graph_rebuild import local_model, revision, resume, objects, read_object
from pipeline.knowledge import KnowledgeEngine
from novel_llm.admission import ollama_session
from pipeline.inference_runtime import preflight


async def probe_names(db,cfg,base,dataset,model,output_path,chapter):
    """One discovery call, never an identity/fact qualification or publication."""
    identity=await local_model(cfg,model)
    r=await revision(db,base)
    c=next((c for c in r['snapshot']['chapters'] if c['chapter']==chapter),None)
    if c is None:
        raise ValueError('chapter is not in the saved completed snapshot')
    source=await asyncio.to_thread(read_object,objects(cfg),cfg,c['raw_uri'])
    reviewed=next((c for c in dataset['chapters'] if c['chapter']==chapter),None)
    if digest(source)!=c['source_hash'] or not reviewed or reviewed['source']!=source:
        raise ValueError('saved source, snapshot and reviewed fixture must agree')
    key=digest(['discovery-only',identity,PROMPT_VERSION,c,r['ontology']])
    found=await(await db.execute("SELECT id FROM graph_revision WHERE evaluation->>'discovery_key'=%s ORDER BY created_at LIMIT 1",(key,))).fetchone()
    if found:
        rid=str(found[0])
    else:
        rid=str((await(await db.execute('''INSERT INTO graph_revision(novel_id,ontology,model,snapshot,evaluation,prompt_version)
            VALUES(%s,%s,%s,%s,%s,%s) RETURNING id''',(r['novel_id'],Jsonb(r['ontology']),Jsonb(identity),
            Jsonb(dict(r['snapshot'],chapters=[c])),Jsonb(dict(discovery_key=key)),PROMPT_VERSION))).fetchone())[0])
    engine=KnowledgeEngine(db,cfg,await revision(db,rid))
    started=time.monotonic()
    try:
        names,mentions,coverage=await engine.discover_names(r['novel_id'],chapter,source)
        if await local_model(cfg,model)!=identity:
            raise ValueError('model changed during discovery probe')
        detected={m['id']:m for m in mentions}
        cases=[c for c in dataset['mentions'] if c['chapter']==chapter and c['unambiguous']]
        hits=sum(c['id'] in detected for c in cases)
        typed_hits=sum(c['id'] in detected and detected[c['id']]['kind']==c['kind'] for c in cases)
        result=dict(status='completed',names=names.model_dump(),coverage_by_kind=coverage,
                    source_occurrences=len(mentions),reviewed_unambiguous_occurrences=len(cases),
                    detected_reviewed_occurrences=hits,discovery_recall=hits/len(cases) if cases else None,
                    correctly_typed_reviewed_occurrences=typed_hits,typed_discovery_recall=typed_hits/len(cases) if cases else None,
                    kind_mismatches=[dict(surface=c['surface'],expected=c['kind'],actual=detected[c['id']]['kind']) for c in cases
                                     if c['id'] in detected and detected[c['id']]['kind']!=c['kind']],
                    missing_reviewed_surfaces=sorted({c['surface'] for c in cases if c['id'] not in detected}))
    except Exception as exc:
        result=dict(status='failed',error_type=type(exc).__name__,error=str(exc))
    finally:
        await engine.close()
    report=dict(revision=rid,model=identity,prompt_version=PROMPT_VERSION,chapter=chapter,
        scope='name discovery only; no identity resolution, claim extraction, alignment or qualification',
        publication_review_complete=False,activation_eligible=False,wall_seconds=time.monotonic()-started,**result)
    await db.execute('UPDATE graph_revision SET evaluation=%s WHERE id=%s',
        (Jsonb(dict(discovery_key=key,**report)),rid))
    Path(output_path).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k!='names'},ensure_ascii=False),flush=True)


async def benchmark(db,cfg,base,dataset,model,output_path):
    identity=await local_model(cfg,model)
    r=await revision(db,base)
    chapters={c['chapter'] for c in dataset['chapters']}
    snapshot=dict(r['snapshot'],chapters=[c for c in r['snapshot']['chapters'] if c['chapter'] in chapters])
    key=digest([identity,dataset,snapshot,r['ontology'],PROMPT_VERSION])
    existing=await(await db.execute("SELECT id FROM graph_revision WHERE evaluation->>'benchmark_key'=%s ORDER BY created_at LIMIT 1",(key,))).fetchone()
    if existing:
        rid=str(existing[0])
    else:
        async with db.transaction():
            rid=str((await(await db.execute('''INSERT INTO graph_revision(novel_id,ontology,model,snapshot,evaluation,prompt_version)
                VALUES(%s,%s,%s,%s,%s,%s) RETURNING id''',(r['novel_id'],Jsonb(r['ontology']),Jsonb(identity),Jsonb(snapshot),Jsonb(dict(benchmark_key=key)),PROMPT_VERSION))).fetchone())[0])
            for c in snapshot['chapters']:
                await db.execute('''INSERT INTO graph_job(revision_id,chapter_index,input_hash,model_identity,generation)
                    VALUES(%s,%s,%s,%s,1)''',(rid,c['chapter'],digest(c),digest(identity)))
    started=time.monotonic();failures=[]
    # Stop once the best possible remaining recall cannot meet the acceptance gate.
    # Untested cases stay explicitly untested, not misreported as model errors.
    tested_chapters=set()
    try:
        for chapter in sorted(chapters):
            await resume(db,cfg,rid,limit=1)
            done=await(await db.execute("SELECT chapter_index FROM graph_job WHERE revision_id=%s AND state='done'",(rid,))).fetchall()
            tested_chapters={c[0] for c in done}
            known={str(m) for (m,) in await(await db.execute('SELECT mention_id FROM mention_binding WHERE revision_id=%s',(rid,))).fetchall()}
            unambiguous=[c for c in dataset['mentions'] if c['unambiguous']]
            missed=sum(c['chapter'] in tested_chapters and c['id'] not in known for c in unambiguous)
            if 1-missed/len(unambiguous)<.90:
                failures.append('Early stop: even perfect remaining links cannot reach 90% unambiguous recall.')
                break
    except Exception as exc:
        failures.append(type(exc).__name__+': '+str(exc))
    bindings={str(mid):str(eid) for mid,eid in await(await db.execute(
        'SELECT mention_id,entity_id FROM mention_binding WHERE revision_id=%s',(rid,))).fetchall()}
    groups=defaultdict(list)
    for case in dataset['mentions']:
        if case['id'] in bindings:
            groups[bindings[case['id']]].append(case['expected_identity'])
    labels={eid:Counter(values).most_common(1)[0][0] for eid,values in groups.items()}
    outputs={chapter:(output or {}) for chapter,output in await(await db.execute(
        "SELECT chapter_index,output FROM graph_job WHERE revision_id=%s AND state='done'",(rid,))).fetchall()}
    first_identity={}
    for case in dataset['mentions']:
        if case.get('expected_identity') is not None:
            first_identity[case['expected_identity']]=min(case['chapter'],first_identity.get(case['expected_identity'],case['chapter']))
    entity_for_label={label:eid for eid,label in labels.items()}
    retrieval_cases=[]
    for case in dataset['mentions']:
        label=case.get('expected_identity')
        if label is None or case['chapter']<=first_identity.get(label,case['chapter']):
            continue
        expected=entity_for_label.get(label)
        if expected:
            retrieval_cases.append(expected in outputs.get(case['chapter'],{}).get('candidate_ids',{}).get(case['id'],[]))
    results=[]
    for case in dataset['mentions']:
        eid=bindings.get(case['id']);label=labels.get(eid)
        correct=(eid is None if not case['unambiguous'] else eid is not None and label==case['expected_identity'])
        results.append(dict(tested=case['chapter'] in tested_chapters,id=case['id'],chapter=case['chapter'],surface=case['surface'],expected=case['expected_identity'],entity_id=eid,correct=correct))
    predicted=[x for x in results if x['entity_id']]
    unambiguous=[c for c in dataset['mentions'] if c['unambiguous']]
    # Separate verification benchmark, with complete source context through the claim's
    # chapter, never generated translation as factual evidence.
    engine=KnowledgeEngine(db,cfg,await revision(db,rid));fact_results=[]
    try:
        for chapter in dataset['chapters'] if not failures else []:
            cases=[c for c in dataset['facts'] if c['chapter']==chapter['chapter']]
            if not cases:continue
            verdicts=await engine.call('verify',Verification,dict(source=chapter['source'],items=[
                dict(id=c['id'],type='fact',subject=c['subject'],value=c['claim'],quote=c['quote']) for c in cases]))
            counts=Counter(v.id for v in verdicts.verdicts)
            votes={v.id:v.supported for v in verdicts.verdicts if counts[v.id]==1}
            fact_results += [dict(id=c['id'],expected=c['expected_supported'],published=votes.get(c['id'],False)) for c in cases]
    except Exception as exc:
        failures.append(str(exc))
    finally:
        await engine.close()
    published=[c for c in fact_results if c['published']]
    metrics=dict(benchmark_key=key,reviewed=True,reviewed_mentions=len(results),reviewed_facts=len(dataset['facts']),tested_mentions=sum(x['tested'] for x in results),tested_facts=len(fact_results),
        link_precision=sum(x['correct'] for x in predicted)/len(predicted) if predicted else None,
        unambiguous_recall=sum(x['correct'] for x in results if x['expected'] is not None)/len(unambiguous),
        recall_upper_bound=(sum(x['correct'] for x in results if x['expected'] is not None)+sum(not x['tested'] for x in results if x['expected'] is not None))/len(unambiguous),
        fact_precision=sum(x['expected'] for x in published)/len(published) if published else None,
        merge_regressions=sum(len(set(v))>1 for v in groups.values()),
        candidate_retrieval_cases=len(retrieval_cases),
        candidate_recall=sum(retrieval_cases)/len(retrieval_cases) if retrieval_cases else 1,
        evidence_valid=not failures,run_wall_seconds=time.monotonic()-started,
        latency_note='Resume/cache wall time; not a cold model latency comparison.',
        # Deliberately cannot authorize activation: a verifier classification benchmark
        # is not an independent review of all facts generated in the full rebuild.
        publication_review_complete=False,failures=failures,early_stopped=bool(failures),fact_verification_status='skipped after identity failure' if failures and not fact_results else 'tested')
    timing=await(await db.execute('SELECT count(*),count(elapsed_seconds),sum(elapsed_seconds) FROM graph_completion WHERE revision_id=%s',(rid,))).fetchone()
    metrics['model_inference_seconds']=timing[2] if timing[0] and timing[0]==timing[1] else None
    metrics['runtime_calls']=[dict(cache_key=key,**(runtime or {})) for key,runtime in await(await db.execute(
        'SELECT cache_key,runtime_metrics FROM graph_completion WHERE revision_id=%s ORDER BY cache_key',(rid,))).fetchall()]
    await db.execute('UPDATE graph_revision SET evaluation=%s WHERE id=%s',(Jsonb(metrics),rid))
    report=dict(revision=rid,model=identity,metrics=metrics,mentions=results,facts=fact_results)
    Path(output_path).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(dict(model=model,revision=rid,metrics=metrics)),flush=True)


async def main(args):
    cfg=Config.load()
    # Validate endpoint/model/settings before waiting. No model inference in preflight.
    diagnostics=await preflight(cfg,args.model)
    if args.preflight:
        async with ollama_session(cfg.ollama_host,timeout=0):
            print(json.dumps(dict(diagnostics,book_admission_available=True),indent=2),flush=True)
        return
    dataset=json.loads(Path(args.dataset).read_text())
    print(json.dumps(dict(event='waiting_for_exclusive_ollama',preflight=diagnostics)),flush=True)
    # Same-task nested provider calls reuse this reservation. Translation/Ask AI
    # back off using AdmissionRejected; no chapter claim is lost or marked failed.
    async with ollama_session(cfg.ollama_host,timeout=cfg.graph_ollama_total_timeout_seconds) as waited:
        print(json.dumps(dict(event='exclusive_ollama_acquired',wait_seconds=waited)),flush=True)
        async with await psycopg.AsyncConnection.connect(cfg.database_url,autocommit=True) as db:
            if args.names_only:
                await probe_names(db,cfg,args.base,dataset,args.model,args.output,args.chapter)
            else:
                await benchmark(db,cfg,args.base,dataset,args.model,args.output)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--base');p.add_argument('--dataset')
    p.add_argument('--model',required=True,choices=['llama3.2:3b','qwen2.5:7b-instruct'])
    p.add_argument('--output')
    p.add_argument('--preflight',action='store_true',help='read metadata and test admission only; no inference or database writes')
    p.add_argument('--names-only',action='store_true',help='one chapter discovery diagnostic; never qualifies a graph')
    p.add_argument('--chapter',type=int,default=1,help='chapter for --names-only (default: 1)')
    args=p.parse_args()
    if args.names_only and args.preflight:
        p.error('--names-only and --preflight are mutually exclusive')
    if not args.preflight and not all([args.base,args.dataset,args.output]):
        p.error('--base, --dataset and --output are required unless --preflight is used')
    asyncio.run(main(args))
