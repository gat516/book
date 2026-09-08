"""Benchmark installed local models on the same reviewed, saved-book cases.

Evaluation revisions are isolated staging graphs, never reader selectable. This
module never calls publish/activate/adopt/quarantine or the deleted whole-revision
review gate (`qualified()`/`record_review()`, removed in Phase E) -- a benchmark run
must never publish or qualify a graph (plan Phase F.0/F.4 "integrity rules").

Ported for the Phase B merged extract pass (`547d383`/`45b5482`): `discover_names()`
now returns `(mentions, coverage, rejected)` off the single 'extract' call instead of
a separate 'names' stage, and there is no surviving `_verify_facts()` -- B.6 drops
fact_verify/fact_review/render/render_verify entirely, so fact quality is no longer
a model-scored precision number. Per plan Phase F.4: "Historical verifier-
classification precision must not be relabeled extraction precision." Fact
precision/recall here are therefore human-review hooks (`reviewed_fact_precision`,
`reviewed_fact_recall`) that return None until a human supplies verdicts over the
actual published rows -- exactly the labeled-assertions-plus-generated-publications
shape plan Phase F.4 asks the report to keep, never a number this script invents.
"""
import argparse
import asyncio
from collections import Counter, defaultdict
from dataclasses import replace
import json
from pathlib import Path
import signal
import time

import psycopg
from psycopg.types.json import Jsonb

from pipeline.config import Config
from pipeline.evidence import Verification, digest, PROMPT_VERSION
from pipeline.graph_rebuild import local_model, revision, resume, objects, read_object
from pipeline.knowledge import KnowledgeEngine
from pipeline.knowledge_contract import materialize_verification, unique_json_object, verification_schema
from pipeline.llm.ollama import OllamaProvider
from pipeline.llm.provider import Class
from pipeline.passages import PassageContract
from novel_llm.admission import ollama_session
from pipeline.inference_runtime import preflight


async def probe_names(db,cfg,base,dataset,model,output_path,chapter):
    """One discovery call, never an identity/fact qualification or publication.

    B.6: names[] now comes off the merged 'extract' pass, so this drives
    `engine.discover_names()` for its (mentions, coverage, rejected) return --
    the pre-Phase-B 3-tuple was (names, mentions, coverage); there is no separate
    'names' model object anymore, so the raw proposal inventory reported below comes
    from `engine._extract_proposals`, the attribute discover_names leaves behind for
    exactly this kind of introspection (knowledge.py:_merged_extract/discover_names).
    """
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
        mentions,coverage,extract_rejected=await engine.discover_names(r['novel_id'],chapter,source)
        if await local_model(cfg,model)!=identity:
            raise ValueError('model changed during discovery probe')
        detected={m['id']:m for m in mentions}
        cases=[c for c in dataset['mentions'] if c['chapter']==chapter and c['unambiguous']]
        hits=sum(c['id'] in detected for c in cases)
        typed_hits=sum(c['id'] in detected and detected[c['id']]['kind']==c['kind'] for c in cases)
        result=dict(status='completed',
                    # Raw model output for a human to eyeball, replacing the old
                    # discrete 'names' pydantic dump: attributes/relations/occurrences
                    # proposed in the SAME call as names[] (B.1), so they're reported
                    # together rather than pretending only names[] happened.
                    extract_proposals=getattr(engine,'_extract_proposals',{}),
                    extract_rejected=extract_rejected,coverage_by_kind=coverage,
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
        # Phase E deleted qualified()/activation_eligible/review_hash along with the
        # whole-revision review gate; this probe never touched that gate even before
        # Phase E (see the original `publication_review_complete=False`), so the only
        # change here is dropping fields that named a concept that no longer exists.
        scope='name/attribute/relation/occurrence discovery only; no identity resolution, '
              'verification, alignment, publication, or qualification of any kind',
        wall_seconds=time.monotonic()-started,**result)
    await db.execute('UPDATE graph_revision SET evaluation=%s WHERE id=%s',
        (Jsonb(dict(discovery_key=key,**report)),rid))
    Path(output_path).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k!='extract_proposals'},ensure_ascii=False),flush=True)


def reviewed_fact_recall(expected, publications, review=None):
    """Recall requires semantic judgments of actual fact rows, never quote overlap."""
    if not expected:
        return None
    if not publications:
        return 0.0
    if review is None:
        return None
    gold={c['id'] for c in expected}
    actual={str(p['id']) for p in publications}
    if set(review)!=gold:
        raise ValueError('fact recall review must assess every expected fact')
    if any(not isinstance(ids,list) or not set(ids)<=actual for ids in review.values()):
        raise ValueError('fact recall review references unpublished facts')
    return sum(bool(ids) for ids in review.values())/len(gold)


def reviewed_fact_precision(publications, review=None):
    """Precision over actually PUBLISHED facts -- the F.4 replacement for the deleted
    `_verify_facts()`-scored precision.

    B.6 dropped fact_verify/fact_review/render/render_verify: structural validation
    (evidence.validate_proposals) plus human review (Phase D's held/review_state) is
    now the semantic approval boundary, not a model verifier. There is therefore no
    automatic score to compute; `review` is an explicit `{fact_id: bool}` map a human
    supplies out of band (same shape as knowledge-repair.md's review document), and
    this returns None -- not 0, not a stand-in verifier score -- until they do.
    """
    if not publications:
        return None
    if review is None:
        return None
    actual={str(p['id']) for p in publications}
    if set(review)!=actual:
        raise ValueError('fact precision review must assess every published fact')
    return sum(bool(v) for v in review.values())/len(review)


def summarize_runtime_calls(rows):
    """Aggregate efficiency without hiding the per-call evidence used to derive it."""
    result={}
    for stage,runtime in rows:
        runtime=runtime or {}
        summary=result.setdefault(stage or 'unknown',dict(
            calls=0,input_tokens=0,output_tokens=0,inference_seconds=0.0,stall_retries=0))
        summary['calls']+=1
        summary['input_tokens']+=runtime.get('input_tokens') or 0
        summary['output_tokens']+=runtime.get('output_tokens') or 0
        summary['inference_seconds']+=runtime.get('request_seconds') or runtime.get('total_seconds') or 0
        summary['stall_retries']+=runtime.get('stall_retries') or 0
    for summary in result.values():
        summary['inference_seconds']=round(summary['inference_seconds'],3)
    return result


def aggregate_call_accounting(chapter_diagnostics):
    """Roll the per-chapter `extract()` diagnostics (knowledge.py:_runtime_diagnostics)
    into a whole-run call-count story.

    Plan Phase B.4's central claim is a drop from 126-134 calls/chapter to a ~4-call
    target (extract, identity_slots, verify, align) "only when each fits" -- bisection/
    saturation splits are the net, not the normal path. This is where that claim gets
    confirmed or refuted: fresh (cold) calls are distinguished from completion-cache
    hits per stage, and every split/saturation/coverage-failure counter Phase B.5 wired
    into `_runtime_diagnostics` is summed here rather than left buried per chapter.
    """
    stages=set();requests=Counter();cache_hits=Counter()
    extract_subdivisions=extract_context_splits=alignment_subdivisions=0
    extract_saturation=Counter();identity_context_splits=0;identity_batch_sizes=[]
    for diag in chapter_diagnostics.values():
        for stage,count in (diag.get('stage_requests') or {}).items():
            stages.add(stage);requests[stage]+=count
        for stage,count in (diag.get('stage_cache_hits') or {}).items():
            stages.add(stage);cache_hits[stage]+=count
        chunking=diag.get('extract_chunking') or {}
        extract_subdivisions+=chunking.get('subdivisions',0)
        extract_context_splits+=chunking.get('context_splits',0)
        for name,count in (chunking.get('saturation') or {}).items():
            extract_saturation[name]+=count
        alignment_subdivisions+=diag.get('alignment_subdivisions',0)
        identity_chunking=diag.get('identity_chunking') or {}
        identity_context_splits+=identity_chunking.get('context_splits',0)
        identity_batch_sizes+=list(identity_chunking.get('batch_sizes',[]))
    fresh={stage:requests[stage]-cache_hits.get(stage,0) for stage in stages}
    chapters=len(chapter_diagnostics)
    return dict(chapters=chapters,stage_requests=dict(requests),stage_cache_hits=dict(cache_hits),
        stage_fresh_calls=fresh,total_requests=sum(requests.values()),
        total_cache_hits=sum(cache_hits.values()),total_fresh_calls=sum(fresh.values()),
        mean_requests_per_chapter=(sum(requests.values())/chapters if chapters else None),
        # Baseline this replaces (plan Context table): 126-134 calls for one chapter.
        baseline_calls_per_chapter=126,
        extract_subdivisions=extract_subdivisions,extract_context_splits=extract_context_splits,
        extract_saturation=dict(extract_saturation),identity_context_splits=identity_context_splits,
        identity_batch_sizes=identity_batch_sizes,alignment_subdivisions=alignment_subdivisions)


def _identity_path_metrics(results, retrieval_hits, groups):
    """One path's (overall / exact-alias fast-path / model-proposed) identity slice.

    F.4: "a separate breakdown for the exact-alias proposal path" -- B.6's two-tier
    identity resolution lets a sole same-kind retrieved candidate skip the
    identity_slots model call (knowledge.py:_resolve_incremental's `fast_ids`), but the
    decision still passes the same semantic verifier before it binds. Its error profile
    (does skipping the proposal call correlate with more merge regressions or lower
    precision?) needs to be visible on its own, not folded into one aggregate number.
    """
    predicted=[r for r in results if r['entity_id']]
    unambiguous=[r for r in results if r['expected'] is not None]
    return dict(mentions=len(results),predicted=len(predicted),
        link_precision=sum(r['correct'] for r in predicted)/len(predicted) if predicted else None,
        unambiguous_recall=sum(r['correct'] for r in unambiguous)/len(unambiguous) if unambiguous else None,
        candidate_retrieval_cases=len(retrieval_hits),
        candidate_recall=sum(retrieval_hits)/len(retrieval_hits) if retrieval_hits else None,
        merge_regressions=sum(len(set(v))>1 for v in groups.values()))


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
    await db.execute("UPDATE graph_revision SET evaluation=evaluation || %s WHERE id=%s",
                     (Jsonb(dict(run_status='running',failures=[])),rid))
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
    outputs={chapter:(output or {}) for chapter,output in await(await db.execute(
        "SELECT chapter_index,output FROM graph_job WHERE revision_id=%s AND state='done'",(rid,))).fetchall()}
    # B.6 two-tier identity (knowledge.py:_resolve_incremental): a resolved mention's
    # published identity item carries 'identity:fast:N' (skipped the identity_slots
    # call) or 'identity:N' (went through the batched model proposal). Both still pass
    # the same semantic verifier -- this only tags which path *proposed* the decision.
    fast_by_mention={}
    for output in outputs.values():
        for item in output.get('items',[]):
            item_id=item.get('id','')
            if item_id.startswith('identity:'):
                fast_by_mention[item['mention_id']]=item_id.startswith('identity:fast:')
    groups=defaultdict(list)
    for case in dataset['mentions']:
        if case['id'] in bindings:
            groups[bindings[case['id']]].append(case['expected_identity'])
    labels={eid:Counter(values).most_common(1)[0][0] for eid,values in groups.items()}
    first_identity={}
    for case in dataset['mentions']:
        if case.get('expected_identity') is not None:
            first_identity[case['expected_identity']]=min(case['chapter'],first_identity.get(case['expected_identity'],case['chapter']))
    entity_for_label={label:eid for eid,label in labels.items()}
    retrieval_rows=[]
    for case in dataset['mentions']:
        label=case.get('expected_identity')
        if label is None or case['chapter']<=first_identity.get(label,case['chapter']):
            continue
        expected=entity_for_label.get(label)
        if expected:
            retrieval_rows.append(dict(id=case['id'],
                hit=expected in outputs.get(case['chapter'],{}).get('candidate_ids',{}).get(case['id'],[])))
    results=[]
    for case in dataset['mentions']:
        eid=bindings.get(case['id']);label=labels.get(eid)
        correct=(eid is None if not case['unambiguous'] else eid is not None and label==case['expected_identity'])
        results.append(dict(tested=case['chapter'] in tested_chapters,id=case['id'],chapter=case['chapter'],
            surface=case['surface'],expected=case['expected_identity'],entity_id=eid,correct=correct,
            fast_path=fast_by_mention.get(case['id'])))
    predicted=[x for x in results if x['entity_id']]
    unambiguous=[c for c in dataset['mentions'] if c['unambiguous']]
    fast_results=[x for x in results if x['fast_path'] is True]
    batch_results=[x for x in results if x['fast_path'] is False]
    fast_groups=defaultdict(list);batch_groups=defaultdict(list)
    for case in dataset['mentions']:
        if case['id'] not in bindings:
            continue
        path=fast_by_mention.get(case['id'])
        if path is True:
            fast_groups[bindings[case['id']]].append(case['expected_identity'])
        elif path is False:
            batch_groups[bindings[case['id']]].append(case['expected_identity'])
    identity_by_path=dict(
        fast_path=_identity_path_metrics(fast_results,[row['hit'] for row in retrieval_rows if fast_by_mention.get(row['id']) is True],fast_groups),
        model_proposed=_identity_path_metrics(batch_results,[row['hit'] for row in retrieval_rows if fast_by_mention.get(row['id']) is False],batch_groups),
        # A mention absent here was rejected before either path bound it (invalid
        # occurrence, unresolved verdict, etc); it is still counted in the overall
        # numbers above, just not attributable to a proposal path.
        unattributed_mentions=sum(1 for r in results if r['entity_id'] and r['fast_path'] is None))
    retrieval_cases=[row['hit'] for row in retrieval_rows]
    # F.4: review actual emitted facts, relations AND events, not facts alone -- B.1's
    # three routing shapes all publish independently (fact/edge/event). Facts stay the
    # only ones with reviewed expected-assertion labels (book-reviewed.json); edges and
    # events are reported for the same human read, unlabeled.
    publication_rows=await(await db.execute('''SELECT f.id::text,f.entity_id::text,e.canonical,
        f.source_chapter,f.attribute,f.value,f.value_en,v.quote,f.review_state
        FROM fact f JOIN entity e ON e.id=f.entity_id
        JOIN graph_evidence v ON v.id=f.evidence_id WHERE f.revision_id=%s ORDER BY f.id''',(rid,))).fetchall()
    publications=[dict(zip(['id','entity_id','subject','chapter','attribute','value','value_en','quote','review_state'],row))
                  for row in publication_rows]
    relation_rows=await(await db.execute('''SELECT ed.id::text,se.canonical,de.canonical,ed.rel_type,
        ed.sentiment,ed.source_chapter,v.quote,ed.review_state
        FROM edge ed JOIN entity se ON se.id=ed.src_id JOIN entity de ON de.id=ed.dst_id
        JOIN graph_evidence v ON v.id=ed.evidence_id WHERE ed.revision_id=%s ORDER BY ed.id''',(rid,))).fetchall()
    relation_publications=[dict(zip(['id','src','dst','rel_type','sentiment','chapter','quote','review_state'],row))
                           for row in relation_rows]
    event_rows=await(await db.execute('''SELECT ev.id::text,ev.chapter_index,ev.summary,ev.entity_ids,
        v.quote,ev.review_state FROM event ev JOIN graph_evidence v ON v.id=ev.evidence_id
        WHERE ev.revision_id=%s ORDER BY ev.id''',(rid,))).fetchall()
    event_publications=[dict(id=row[0],chapter=row[1],summary=row[2],
        participant_ids=[str(x) for x in (row[3] or [])],quote=row[4],review_state=row[5])
        for row in event_rows]
    expected_facts=[c for c in dataset['facts'] if c['expected_supported']]
    metrics=dict(benchmark_key=key,reviewed=True,reviewed_mentions=len(results),reviewed_facts=len(dataset['facts']),
        tested_mentions=sum(x['tested'] for x in results),
        # Redefined from "facts the deleted verifier scored" to "facts whose chapter
        # actually ran" -- there is no model verifier left to score them (B.6).
        tested_facts=sum(1 for c in dataset['facts'] if c['chapter'] in tested_chapters),
        link_precision=sum(x['correct'] for x in predicted)/len(predicted) if predicted else None,
        unambiguous_recall=sum(x['correct'] for x in results if x['expected'] is not None)/len(unambiguous),
        recall_upper_bound=(sum(x['correct'] for x in results if x['expected'] is not None)+sum(not x['tested'] for x in results if x['expected'] is not None))/len(unambiguous),
        identity_by_path=identity_by_path,
        # No automated fact_precision/recall survive Phase B (see module docstring):
        # both are None until a human supplies `review` over these `publications`.
        fact_precision=reviewed_fact_precision(publications),
        end_to_end_fact_recall=reviewed_fact_recall(expected_facts,publications),
        fact_recall_status='awaiting human review of published facts' if publications else 'no published facts',
        merge_regressions=sum(len(set(v))>1 for v in groups.values()),
        candidate_retrieval_cases=len(retrieval_cases),
        candidate_recall=sum(retrieval_cases)/len(retrieval_cases) if retrieval_cases else 1,
        evidence_valid=not failures,run_wall_seconds=time.monotonic()-started,
        latency_note='Resume/cache wall time; not a cold model latency comparison.',
        # Phase E deleted the whole-revision review gate (qualified()/record_review());
        # this benchmark never qualified or activated anything even before that, so the
        # only change is not naming a gate that no longer exists.
        failures=failures,early_stopped=bool(failures),
        run_status='failed' if failures else 'completed')
    # KnowledgeEngine's production cache is novel-scoped and cross-revision. Attribute
    # each distinct cached inference through completion_cache_run, then join back to the
    # durable response row for timings. The old graph_completion table is intentionally
    # read-only after 0068 and cannot describe this benchmark.
    runtime_rows=await(await db.execute('''SELECT DISTINCT ON (c.novel_id,c.cache_key,c.served_provider,c.served_model)
            c.cache_key,c.stage,c.runtime_metrics,c.elapsed_seconds
        FROM completion_cache c
        JOIN completion_cache_run u
          ON u.cache_key=c.cache_key AND u.served_provider=c.served_provider
         AND u.served_model=c.served_model
        JOIN graph_revision r ON r.id=u.revision_id AND r.novel_id=c.novel_id
        WHERE u.revision_id=%s
        ORDER BY c.novel_id,c.cache_key,c.served_provider,c.served_model,c.created_at''',(rid,))).fetchall()
    elapsed=[row[3] for row in runtime_rows]
    metrics['model_inference_seconds']=(sum(elapsed)
        if elapsed and all(value is not None for value in elapsed) else None)
    metrics['runtime_calls']=[dict(cache_key=key,elapsed_seconds=elapsed_seconds,**(runtime or {}))
                              for key,_stage,runtime,elapsed_seconds in runtime_rows]
    metrics['runtime_by_stage']=summarize_runtime_calls([(stage,runtime) for _key,stage,runtime,_elapsed in runtime_rows])
    chapter_diagnostics={c:o.get('diagnostics',{}) for c,o in outputs.items()}
    # F.4/B.4: the per-stage accounting that confirms or refutes "126 calls -> ~4" --
    # cold calls, cache hits, retries (stall_retries, in runtime_by_stage above), and
    # every saturation/bisection split summed across the tested chapters.
    metrics['call_accounting']=aggregate_call_accounting(chapter_diagnostics)
    coverage_failure_rows=await(await db.execute('''SELECT a.chapter_index,a.payload
        FROM chapter_knowledge_activity a JOIN chapter_knowledge_run cr ON cr.id=a.run_id
        WHERE cr.revision_id=%s AND a.item_kind='run' AND a.phase='rejected'
          AND a.payload->>'reason' ILIKE %s ORDER BY a.chapter_index''',
        (rid,'%coverage failure%'))).fetchall()
    metrics['coverage_failures']=[dict(chapter=chapter,**(payload or {})) for chapter,payload in coverage_failure_rows]
    await db.execute('UPDATE graph_revision SET evaluation=%s WHERE id=%s',(Jsonb(metrics),rid))
    report=dict(revision=rid,model=identity,metrics=metrics,mentions=results,
                publications=publications,relation_publications=relation_publications,
                event_publications=event_publications,expected_facts=expected_facts,
                chapter_diagnostics=chapter_diagnostics)
    Path(output_path).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(dict(model=model,revision=rid,metrics=metrics)),flush=True)


async def probe_verifier(cfg,dataset,model,output_path,chapter):
    """Compare verifier wire shapes on identical reviewed facts; never writes graph state.

    NOTE (F.4 port): the fact-shaped payload below (subject/value/evidence) mimics the
    PRE-Phase-B 'verify' contract. B.6 kept 'verify' for identity decisions only (the
    one verifier every binding still passes through, fast-path included) -- production
    no longer sends a fact-shaped item through this stage at all. This probe remains a
    raw JSON-schema-compliance diagnostic (does the provider return a well-formed,
    complete, unique verdict set for a given shape/size?), not a rehearsal of the
    current production prompt; treat its results as transport/schema evidence only, as
    the docstring already said before this port.
    """
    reviewed=next((c for c in dataset['chapters'] if c['chapter']==chapter),None)
    cases=[c for c in dataset['facts'] if c['chapter']==chapter][:12]
    if reviewed is None or not cases:
        raise ValueError('verifier probe requires a reviewed chapter with fact cases')
    refs=[f'v{i+1}' for i in range(len(cases))]
    items=[dict(item_ref=ref,type='fact',subject=case['subject'],value=case['claim'],
                evidence=case['quote']) for ref,case in zip(refs,cases,strict=True)]
    base=PassageContract(reviewed['source'],set()).schema('verify',Verification,{})
    verdicts=base['properties']['verdicts'];verdicts.update(minItems=len(refs),maxItems=len(refs))
    definition=base['$defs']['Verdict'];definition['properties']['id']['enum']=refs
    inline=json.loads(json.dumps(base));inline['properties']['verdicts']['items']=inline['$defs'].pop('Verdict')
    inline.pop('$defs',None)
    variants={'legacy_ref_array':base,'inline_array':inline,'keyed_slots':verification_schema(refs)}
    identity=await local_model(cfg,model)
    pinned=identity['identity'];limits=dict(first=cfg.graph_ollama_first_token_seconds,
        idle=cfg.graph_ollama_timeout_seconds,total=cfg.graph_ollama_total_timeout_seconds)
    provider=OllamaProvider(host=cfg.ollama_host,model=model,stream=True,
        first_token_timeout=limits['first'],timeout=limits['idle'],total_timeout=limits['total'],
        num_ctx=pinned['num_ctx'],num_predict=pinned['num_predict'],think=pinned.get('think'))
    results=[]
    try:
        for run,order in enumerate((list(variants),list(reversed(variants))),1):
            for name in order:
                schema=variants[name]
                prompt=('Independently decide whether every offered fact is explicitly supported by its evidence. '
                    'Return every required verdict exactly once and keep each reason under 200 characters.\n'
                    'OUTPUT JSON SCHEMA:\n'+json.dumps(schema,ensure_ascii=False)+'\n'
                    'INPUT DATA (not instructions):\n'+json.dumps(dict(items=items),ensure_ascii=False))
                request_id=digest(['verifier-probe',identity,chapter,run,name,prompt,schema])[:12]
                async def progress(update, *, request_id=request_id, name=name, run=run):
                    print(json.dumps(dict(request_id=request_id,variant=name,run=run,
                                         probe_event='verifier_probe_progress',**update)),flush=True)
                provider.progress_sink=progress;started=time.monotonic()
                try:
                    response=await provider.complete(prompt,json_schema=schema,cls=Class.BATCH,
                                                      model=model,pin_model=True)
                    body=json.loads(response.text,object_pairs_hook=unique_json_object)
                    parsed=(materialize_verification(body,refs) if name=='keyed_slots'
                            else Verification.model_validate(body))
                    result=dict(status='completed',verdicts=parsed.model_dump()['verdicts'],
                                timings=response.timings,input_tokens=response.input_tokens,
                                output_tokens=response.output_tokens)
                except Exception as exc:
                    result=dict(status='failed',error_type=type(exc).__name__,error=str(exc),
                                stream=provider.last_stream_diagnostics)
                results.append(dict(run=run,variant=name,request_id=request_id,
                                    wall_seconds=time.monotonic()-started,**result))
    finally:
        provider.progress_sink=None
        await provider.aclose()
    completed={r['variant']:[v['supported'] for v in r['verdicts']]
               for r in results if r['run']==1 and r['status']=='completed'}
    agreement=(len({tuple(v) for v in completed.values()})==1
               if len(completed)==len(variants) else None)
    artifact=dict(scope='operator-only verifier diagnostic; cannot qualify or activate a revision',
        chapter=chapter,source_hash=digest(reviewed['source']),model=identity,limits=limits,
        payload=dict(items=items),schemas=variants,first_run_verdict_agreement=agreement,results=results)
    Path(output_path).write_text(json.dumps(artifact,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(dict(event='verifier_probe_completed',output=output_path,results=results)),flush=True)


async def main(args):
    cfg=Config.load()
    if not args.preflight and not args.names_only:
        # A benchmark is diagnostic: one pathological structured response must finish or
        # fail quickly enough to compare contracts. Production defaults remain untouched.
        cfg=replace(cfg,graph_ollama_first_token_seconds=180,
                    graph_ollama_timeout_seconds=60,
                    graph_ollama_total_timeout_seconds=300)
    loop=asyncio.get_running_loop();task=asyncio.current_task()
    installed=[]
    for sig in (signal.SIGINT,signal.SIGTERM):
        try:
            loop.add_signal_handler(sig,task.cancel);installed.append(sig)
        except (NotImplementedError,RuntimeError):
            pass
    try:
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
        async with ollama_session(cfg.ollama_host,timeout=cfg.graph_ollama_total_timeout_seconds or 1800) as waited:
            print(json.dumps(dict(event='exclusive_ollama_acquired',wait_seconds=waited)),flush=True)
            async with await psycopg.AsyncConnection.connect(cfg.database_url,autocommit=True) as db:
                if args.verify_probe:
                    await probe_verifier(cfg,dataset,args.model,args.output,args.chapter)
                elif args.names_only:
                    await probe_names(db,cfg,args.base,dataset,args.model,args.output,args.chapter)
                else:
                    await benchmark(db,cfg,args.base,dataset,args.model,args.output)
    finally:
        for sig in installed:
            loop.remove_signal_handler(sig)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--base');p.add_argument('--dataset')
    # Installed-model discovery in local_model() is the authority. A hard-coded CLI
    # allowlist made a pinned, locally installed Qwen3 revision impossible to qualify.
    p.add_argument('--model',required=True)
    p.add_argument('--output')
    p.add_argument('--preflight',action='store_true',help='read metadata and test admission only; no inference or database writes')
    p.add_argument('--names-only',action='store_true',help='one chapter discovery diagnostic; never qualifies a graph')
    p.add_argument('--verify-probe',action='store_true',help='compare verifier schemas on reviewed facts; never writes graph state')
    p.add_argument('--chapter',type=int,default=1,help='chapter for --names-only (default: 1)')
    args=p.parse_args()
    if sum((args.names_only,args.verify_probe,args.preflight))>1:
        p.error('--names-only, --verify-probe and --preflight are mutually exclusive')
    if not args.preflight and not all([args.dataset,args.output]):
        p.error('--dataset and --output are required unless --preflight is used')
    if not (args.preflight or args.verify_probe) and not args.base:
        p.error('--base is required for benchmark and --names-only runs')
    asyncio.run(main(args))
