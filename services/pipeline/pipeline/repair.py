"""Execute knowledge-repair intents recorded by the API (migration 0043).

The repair verbs already exist and are carefully defended: ``prepare`` quarantines and
snapshots under a lock, ``resume`` rebuilds a chapter at a time under a model pin,
``preview`` freezes a report, ``record_review`` derives its metrics from per-item
assessments joined against actually-stored bindings, ``switch`` is the only cutover, and
``extend`` raises the chapter ceiling on an already-trusted graph.
Until now they were reachable only from argparse, so repairing a book meant six commands
at a shell.

This module is the bridge, and it is deliberately thin. It claims a ``repair_request``
row and calls those same functions. It re-implements no gate and skips no check:
``qualified`` still decides what may be activated, and a review submitted
from a browser goes through exactly the code a review submitted from a file does.

It runs on the worker's idle tick, below reader-critical translation (§0), because a
rebuild must never delay a chapter someone is waiting to read.
"""
from __future__ import annotations

import asyncio
import json
from copy import deepcopy

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from pipeline.config import Config
from pipeline.failures import ABANDONED, failure_category
from pipeline.llm.provider import AdmissionRejected


async def _reextract_preview(db, cfg, graph_rebuild, row, run_id):
    from pipeline.evidence import digest, stable_id
    run=await(await db.execute('''SELECT r.id::text,r.novel_id::text,r.chapter_index,r.revision_id::text,
        r.input_hash,r.display_hash,r.model_identity,r.graph_generation,r.graph_version,r.scope
        FROM chapter_knowledge_run r WHERE r.id=%s AND r.novel_id=%s AND r.chapter_index=%s
          AND r.mode='reextract' AND r.state IN ('pending','processing')''',
        (run_id,row['novel_id'],row['chapter_index']))).fetchone()
    if not run: raise ValueError('re-extraction run is stale or unavailable')
    _,novel,chapter,rid,input_hash,display_hash,model_identity,generation,version,scope=run
    revision=await graph_rebuild.revision(db,rid)
    if revision['state']!='active' or not revision['trusted'] or revision['legacy'] or revision['generation']!=generation or revision['version']!=version:
        raise ValueError('active graph changed after re-extraction was requested')
    live=await graph_rebuild.local_model(cfg,revision['model']['name'])
    if digest(live)!=model_identity:
        raise ValueError('model identity changed after re-extraction was requested')
    saved=next((c for c in revision['snapshot']['chapters'] if c['chapter']==chapter),None)
    if not saved: raise ValueError('chapter is not in the active graph snapshot')
    client=graph_rebuild.objects(cfg)
    source=await asyncio.to_thread(graph_rebuild.read_object,client,cfg,saved['raw_uri'])
    display=await asyncio.to_thread(graph_rebuild.read_object,client,cfg,saved['translated_uri'] or saved['raw_uri'])
    if digest(source)!=input_hash or digest(display)!=display_hash:
        raise ValueError('chapter input changed after re-extraction was requested')
    lang=(await(await db.execute('SELECT target_lang FROM novel WHERE id=%s',(novel,))).fetchone())[0]
    engine=graph_rebuild.KnowledgeEngine(db,cfg,revision,run_id=run_id)
    try: output=await engine.extract(novel,chapter,source,display,lang,
                                     include_terms=scope != 'facts', include_facts=scope != 'terms')
    finally: await engine.close()
    identities={i['mention_id']:i for i in output['items'] if i['id'].startswith('identity:')}
    resolved={}
    for mid,item in identities.items():
        if item['outcome']=='existing': resolved[mid]=item['target_id']
        elif item['outcome']=='new': resolved[mid]=stable_id(rid,'entity',item['target_id'])
    existing_rows=await(await db.execute('''SELECT f.id,f.entity_id::text,f.attribute,f.value,f.value_en
        FROM fact f WHERE f.revision_id=%s AND f.source_chapter=%s AND f.kind<>'retraction'
        AND NOT EXISTS(SELECT 1 FROM fact s WHERE s.revision_id=f.revision_id AND s.supersedes=f.id)''',(rid,chapter))).fetchall()
    existing=[dict(id=r[0],entity_id=r[1],attribute=r[2],value=r[3],value_en=r[4]) for r in existing_rows] if scope != 'terms' else []
    matched=set();items=[]
    for claim in (i for i in output['items'] if i['id'].startswith('claim:') and i['type']=='fact'):
        entity=resolved.get(claim['mention_ids'][0]); exact=next((f for f in existing if f['entity_id']==entity and f['attribute']==claim['attribute'] and f['value']==claim['value']),None)
        replacement=next((f for f in existing if f['entity_id']==entity and f['attribute']==claim['attribute']),None)
        if exact:
            matched.add(exact['id']); classification='unchanged' if (exact['value_en'] or exact['value'])==(claim.get('value_en') or claim['value']) else 'display_update'; old=exact['id']
        elif replacement: matched.add(replacement['id']);classification='possible_replacement';old=replacement['id']
        else: classification='new';old=None
        items.append(dict(item_key=claim['id'],item_kind='fact',classification=classification,
                          existing_fact_id=old,proposal=claim))
    for fact in existing:
        if fact['id'] not in matched:
            items.append(dict(item_key=f"missing:{fact['id']}",item_kind='fact',classification='missing',existing_fact_id=fact['id'],proposal=None))
    mentions={m['id']:m for m in output['mentions']}
    seen=set()
    for span in output['spans'] if scope != 'facts' else []:
        mid=span.get('mention_id')
        if not mid or mid not in mentions: continue
        source_term=mentions[mid]['surface'];key=(source_term,span['phrase'])
        if key in seen: continue
        seen.add(key)
        glossary=await(await db.execute('SELECT target_term,deleted FROM glossary WHERE novel_id=%s AND source_term=%s',(novel,source_term))).fetchone()
        classification='new' if not glossary or glossary[1] else ('unchanged' if glossary[0]==span['phrase'] else 'display_update')
        items.append(dict(item_key=f'term:{source_term}',item_kind='term',classification=classification,
                          proposal=dict(source_term=source_term,target_term=span['phrase'])))
    preview=dict(run_id=run_id,scope=scope,revision_id=rid,version=version,chapter_index=chapter,
                 model_identity=model_identity,items=items,output=output)
    await db.execute("UPDATE chapter_knowledge_run SET state='awaiting_review',preview=%s,updated_at=now() WHERE id=%s",(Jsonb(preview),run_id))
    await db.execute('''INSERT INTO chapter_knowledge_activity
        (run_id,novel_id,chapter_index,item_kind,item_key,phase,payload,idempotency_key)
        VALUES(%s,%s,%s,'run','run','verified',%s,'run:awaiting-review')
        ON CONFLICT(run_id,idempotency_key) DO NOTHING''',(run_id,novel,chapter,Jsonb({'items':len(items)})))
    return {'run_id':run_id,'state':'awaiting_review','items':len(items)}


async def _reextract_apply(db,cfg,graph_rebuild,row,run_id,decisions):
    from pipeline.context import PipelineState
    from pipeline.envelope import ChapterEnvelope,SourceMeta
    from pipeline.evidence import digest
    run=await(await db.execute('''SELECT r.revision_id::text,r.graph_generation,r.graph_version,r.input_hash,
        r.display_hash,r.model_identity,r.preview,r.requested_by,r.scope FROM chapter_knowledge_run r
        WHERE r.id=%s AND r.novel_id=%s AND r.chapter_index=%s AND r.state='applying' FOR UPDATE''',
        (run_id,row['novel_id'],row['chapter_index']))).fetchone()
    if not run: raise ValueError('re-extraction preview is stale or unavailable')
    rid,generation,version,input_hash,display_hash,model_identity,preview,requested_by,scope=run
    revision=await graph_rebuild.revision(db,rid,lock=True)
    if revision['state']!='active' or not revision['trusted'] or revision['generation']!=generation or revision['version']!=version:
        raise ValueError('active graph changed after preview')
    live=await graph_rebuild.local_model(cfg,revision['model']['name'])
    if digest(live)!=model_identity:
        raise ValueError('model identity changed after preview')
    saved=next((c for c in revision['snapshot']['chapters'] if c['chapter']==row['chapter_index']),None)
    client=graph_rebuild.objects(cfg)
    source=await asyncio.to_thread(graph_rebuild.read_object,client,cfg,saved['raw_uri'])
    display=await asyncio.to_thread(graph_rebuild.read_object,client,cfg,saved['translated_uri'] or saved['raw_uri'])
    if digest(source)!=input_hash or digest(display)!=display_hash: raise ValueError('chapter input changed after preview')
    if isinstance(decisions,list):
        choices={d.get('item_key'):d.get('action','retain') for d in decisions if isinstance(d,dict)}
    elif isinstance(decisions,dict): choices=decisions
    else: raise ValueError('decisions must be an object or list')
    allowed={'retain','approve','update_display','replace','remove'}
    if any(action not in allowed for action in choices.values()): raise ValueError('unknown re-extraction decision')
    items={item['item_key']:item for item in preview['items']}
    if not set(choices)<=set(items): raise ValueError('decision names an unknown preview item')
    if scope != 'terms' and not any(action!='retain' for action in choices.values()):
        await db.execute("UPDATE chapter_knowledge_run SET state='rejected',updated_at=now() WHERE id=%s",(run_id,))
        await db.execute('''INSERT INTO chapter_knowledge_activity
            (run_id,novel_id,chapter_index,item_kind,item_key,phase,payload,idempotency_key)
            VALUES(%s,%s,%s,'run','run','rejected','{"reason":"review completed with no selected changes"}','run:review-retained')
            ON CONFLICT(run_id,idempotency_key) DO NOTHING''',(run_id,row['novel_id'],row['chapter_index']))
        return {'run_id':run_id,'state':'rejected','revision_id':rid,'version':version}
    output=deepcopy(preview['output'])
    selected=[]
    for item in preview['items']:
        if item['item_kind']!='fact': continue
        action=choices.get(item['item_key'],'retain')
        if item['classification']=='new' and action=='approve': selected.append(item['proposal'])
        elif item['classification']=='possible_replacement' and action=='replace':
            proposal=deepcopy(item['proposal']);proposal['kind']='correction';proposal['supersedes']=item['existing_fact_id'];selected.append(proposal)
    output['items']=[i for i in output['items'] if i['id'].startswith('identity:')]+selected
    source_lang,target_lang=(await(await db.execute('SELECT source_lang,target_lang FROM novel WHERE id=%s',(row['novel_id'],))).fetchone())
    state=PipelineState(envelope=ChapterEnvelope(novel_id=row['novel_id'],chapter_index=row['chapter_index'],raw_text=source,source_lang=source_lang,source_meta=SourceMeta()))
    engine=graph_rebuild.KnowledgeEngine(db,cfg,revision,run_id=run_id)
    engine.current_chapter=row['chapter_index']
    try:
        async with db.transaction():
            current=await graph_rebuild.revision(db,rid,lock=True)
            current_input=await(await db.execute('SELECT raw_hash FROM chapter WHERE novel_id=%s AND chapter_index=%s FOR SHARE',(row['novel_id'],row['chapter_index']))).fetchone()
            if current['state']!='active' or not current['trusted'] or current['generation']!=generation or current['version']!=version or not current_input:
                raise ValueError('active graph or chapter changed while applying preview')
            await db.execute("UPDATE chapter_knowledge_run SET state='applying',updated_at=now() WHERE id=%s",(run_id,))
            await db.execute("SELECT set_config('app.graph_revision',%s,true),set_config('app.graph_generation',%s,true)",(rid,str(generation)))
            await engine.publish(state,output,source,publish_terms=scope != 'facts',publish_facts=scope != 'terms')
            if scope != 'facts':
                await graph_rebuild.promote_verified_glossary(db,current,row['chapter_index'],target_lang)
            actor=row.get('requested_by') or requested_by or 'unknown operator'
            for item in preview['items']:
                if item['item_kind']!='fact': continue
                action=choices.get(item['item_key'],'retain');fid=item.get('existing_fact_id')
                if item['classification']=='display_update' and action=='update_display':
                    value_en=item['proposal'].get('value_en') or item['proposal']['value']
                    old=await(await db.execute('SELECT COALESCE(value_en,value) FROM fact WHERE id=%s',(fid,))).fetchone()
                    await db.execute('UPDATE fact SET value_en=%s WHERE id=%s',(value_en,fid))
                    await db.execute('''INSERT INTO fact_edit_audit(novel_id,revision_id,fact_id,action,actor,old_display,new_display,note)
                        VALUES(%s,%s,%s,'display',%s,%s,%s,'approved chapter re-extraction preview')''',(row['novel_id'],rid,fid,actor,old[0],value_en))
                elif item['classification']=='missing' and action=='remove':
                    old=await(await db.execute('''SELECT entity_id,attribute,value,value_en,valid_from_chapter,source_chapter,
                        confidence,evidence_id FROM fact WHERE id=%s''',(fid,))).fetchone()
                    successor=(await(await db.execute('''INSERT INTO fact(novel_id,entity_id,attribute,value,value_en,
                        valid_from_chapter,source_chapter,confidence,kind,supersedes,revision_id,evidence_id,claim_key)
                        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'retraction',%s,%s,%s,%s) RETURNING id''',
                        (row['novel_id'],*old[:7],fid,rid,old[7],digest(['retraction',rid,fid,run_id])))).fetchone())[0]
                    await db.execute('''INSERT INTO fact_edit_audit(novel_id,revision_id,fact_id,action,actor,old_display,successor_fact_id,note)
                        VALUES(%s,%s,%s,'retraction',%s,%s,%s,'approved chapter re-extraction preview')''',
                        (row['novel_id'],rid,fid,actor,old[3] or old[2],successor))
                elif item['classification']=='possible_replacement' and action=='replace':
                    successor=await(await db.execute('''SELECT id,COALESCE(value_en,value) FROM fact
                        WHERE revision_id=%s AND supersedes=%s ORDER BY id DESC LIMIT 1''',(rid,fid))).fetchone()
                    old=await(await db.execute('SELECT COALESCE(value_en,value) FROM fact WHERE id=%s',(fid,))).fetchone()
                    if successor:
                        await db.execute('''INSERT INTO fact_edit_audit(novel_id,revision_id,fact_id,action,actor,
                            old_display,new_display,successor_fact_id,note)
                            VALUES(%s,%s,%s,'correction',%s,%s,%s,%s,'approved chapter re-extraction preview')''',
                            (row['novel_id'],rid,fid,actor,old[0],successor[1],successor[0]))
            new_version=(await(await db.execute('UPDATE graph_revision SET version=version+1,review=NULL WHERE id=%s RETURNING version',(rid,))).fetchone())[0]
            await db.execute("UPDATE chapter_knowledge_run SET state='published',updated_at=now() WHERE id=%s",(run_id,))
    finally: await engine.close()
    return {'run_id':run_id,'state':'published','revision_id':rid,'version':new_version}

# How long a claimed request may stay 'running' before another worker may take it back.
# A worker that dies mid-action leaves state='running', and repair_request_one_active
# covers ('pending','running'), so without a sweep that book's repair is refused with 409
# forever.  graph_job survives the same crash because next_retryable_active_revision
# treats 'processing' as retryable; this is the repair executor's equivalent.
#
# Generous on purpose.  None of these actions run model inference -- prepare only reads
# local model metadata, and preview re-slices stored quotes -- so half an hour is far
# beyond a healthy run while staying well short of "nobody will ever notice".
STALE_AFTER_MINUTES = 30

# Only transient causes are worth retrying.  A malformed review document or a revision
# that fails qualified() will fail identically on every attempt; scheduling three more
# runs of it produces noise, not recovery.  Reusing graph_rebuild's delays keeps one
# backoff policy in the codebase rather than two.
TRANSIENT_CATEGORIES = {"model_unreachable", "timeout"}

# No per-revision attempt counter exists to back off against (unlike graph_job's
# attempts + graph_retry_delay_minutes), so this is a flat cooldown rather than an
# escalating one. resume()'s preamble check is cheap (metadata only, no model call), so a
# flat 5 minutes just bounds how often a still-down endpoint gets re-probed.
BLOCKED_RETRY_MINUTES = 5


def retry_delay_minutes(attempt: int) -> int | None:
    from pipeline.graph_rebuild import graph_retry_delay_minutes

    return graph_retry_delay_minutes(attempt)



async def _reclaim_abandoned(db) -> int:
    """Unblock a book whose request was left 'running' by a dead worker.

    It fails the row; it deliberately does NOT requeue it.  These actions are not
    idempotent: prepare quarantines the active graph and creates a staging revision, so a
    worker that died between finishing prepare and recording the result would, on a
    retry, quarantine a second time and leave a second staging revision behind.  Whether
    that is what the operator wants is a judgement, and the panel already shows enough
    state to make it -- so the row reports honestly and a human decides.

    STALE_AFTER_MINUTES is a heuristic, not a guarantee, so this can fire while an action
    is genuinely still running.  That is safe because execution is additionally guarded by
    the advisory lock in drain_requests: a second worker cannot start the same novel and
    track while the first is inside _run.
    """
    cursor = await db.execute(
        """UPDATE repair_request
              SET state='failed', category=%s, started_at=NULL, updated_at=now()
            WHERE state = 'running'
              AND started_at IS NOT NULL
              AND started_at < now() - (%s * interval '1 minute')""",
        (ABANDONED, STALE_AFTER_MINUTES),
    )
    return cursor.rowcount


async def _claim(db, novel_id: str | None) -> dict | None:
    """Take one pending request, marking it running in the same transaction.

    ``FOR UPDATE SKIP LOCKED`` so two workers never run the same action twice.  The
    partial unique index already caps this at one in-flight request per novel per track;
    this is the second half of that guarantee, on the executor side.
    """
    async with db.transaction():
        # A dict cursor here, not a dict connection: graph_rebuild.prepare and its
        # neighbours index their own rows positionally (row[0]) and open a dict_row
        # cursor only where they want names.  Handing them a dict_row *connection* would
        # break every one of those callers.  revision() sets the same precedent.
        async with db.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """SELECT id::text AS id, novel_id::text AS novel_id, track, action,
                          revision_id::text AS revision_id, params, attempts, chapter_index
                     FROM repair_request
                    WHERE state = 'pending' AND (retry_at IS NULL OR retry_at <= now())
                      AND (%s::uuid IS NULL OR novel_id = %s::uuid)
                    ORDER BY created_at
                    LIMIT 1 FOR UPDATE SKIP LOCKED""",
                (novel_id, novel_id),
            )
            row = await cur.fetchone()
        if not row:
            return None
        await db.execute(
            """UPDATE repair_request SET state='running', attempts=attempts+1, error=NULL,
                      category=NULL, retry_at=NULL, started_at=now(), updated_at=now()
                WHERE id=%s""",
            (row["id"],),
        )
        return row


async def _run(db, cfg, row: dict) -> dict:
    """Dispatch one request to the existing rebuild functions.

    Every branch below is a call into graph_rebuild or event_rebuild with arguments taken
    from the request.  Nothing is decided here.
    """
    from pipeline import event_rebuild, graph_rebuild

    module = graph_rebuild if row["track"] == "graph" else event_rebuild
    params = row["params"] or {}
    action = row["action"]

    if action == "prepare":
        model = params.get("model")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("prepare requires a model name")
        if row["track"] == "graph":
            provider = params.get("provider", "ollama")
            kwargs={"upto_chapter":params.get("upto_chapter")}
            if "provider" in params:
                kwargs["provider"]=provider
            revision = await module.prepare(db, cfg, row["novel_id"], model.strip(),**kwargs)
        else:
            if "upto_chapter" in params:
                raise ValueError("chapter ceilings apply only to the entity graph")
            provider = params.get("provider", "ollama")
            if provider not in event_rebuild.EXTRACTION_PROVIDERS:
                raise ValueError(f"unknown extraction provider {provider!r}")
            revision = await module.prepare(db, cfg, row["novel_id"], model.strip(),
                                            params.get("schema"), provider=provider)
        return {"revision": str(revision)}

    if action == "extend":
        if row["track"] != "graph":
            raise ValueError("extension applies only to the entity graph")
        return await graph_rebuild.extend(db, cfg, row["novel_id"],
                                          upto_chapter=params.get("upto_chapter"))

    if action == "reextract":
        if row["track"] != "graph":
            raise ValueError("re-extraction applies to the entity graph")
        run_id=params.get('run_id')
        if not isinstance(run_id,str) or not run_id:
            raise ValueError('reextract requires a run_id')
        return await _reextract_preview(db,cfg,graph_rebuild,row,run_id)

    if action == 'reextract_apply':
        if row['track']!='graph': raise ValueError('re-extraction applies to the entity graph')
        run_id=params.get('run_id');decisions=params.get('decisions')
        if not isinstance(run_id,str) or not run_id: raise ValueError('reextract_apply requires a run_id')
        return await _reextract_apply(db,cfg,graph_rebuild,row,run_id,decisions)

    if not row["revision_id"]:
        raise ValueError(f"{action} requires a revision")

    if action == "review":
        document = params.get("document")
        if not isinstance(document, dict):
            raise ValueError("review requires a document")
        # record_review recomputes the report, matches its hash, requires every published
        # claim to be assessed exactly once, and derives the scores itself.  A browser
        # submits the same document a file would.
        return await module.record_review(db, cfg, row["revision_id"], document)

    if action == "activate":
        review_hash = params.get("review_hash")
        if not isinstance(review_hash, str) or not review_hash:
            raise ValueError("activate requires the review hash from the recorded review")
        await module.switch(db, cfg, row["revision_id"], review_hash)
        return {"status": "activated", "revision": row["revision_id"]}

    if action == "rollback":
        await module.switch(db, cfg, row["revision_id"], rollback=True)
        # Rollback deliberately preserves trust: returning to a contaminated revision does
        # not restore its facts.  Say so in the result rather than letting the UI imply
        # that rolling back fixed anything.
        return {"status": "rolled back", "revision": row["revision_id"],
                "note": "trust is not restored by rollback; facts stay withheld"}

    if action == "retry":
        # A blocked revision otherwise clears itself only for TRANSIENT_CATEGORIES, and
        # only after BLOCKED_RETRY_MINUTES (see _next_staging_revision). This lets an
        # operator who has fixed the actual cause -- restarted the tunnel, installed the
        # pinned model, added a credential -- unstick it immediately instead of waiting
        # out a cooldown that assumes nothing changed. resume()'s preamble re-checks
        # everything before spending a call, so clearing the marker here costs nothing if
        # the cause has not actually cleared; it just re-blocks with a fresh blocked_at.
        table = "graph_revision" if row["track"] == "graph" else "event_revision"
        before = await (await db.execute(
            f"SELECT blocked_category FROM {table} WHERE id=%s AND state='staging'",
            (row["revision_id"],))).fetchone()
        if not before:
            raise ValueError("no such staging rebuild to retry")
        await db.execute(
            f"UPDATE {table} SET blocked_category=NULL, blocked_at=NULL WHERE id=%s",
            (row["revision_id"],))
        return {"status": "retry requested", "revision": row["revision_id"],
                "was_blocked": before[0] is not None}

    if action == "discard":
        if row["track"] != "graph":
            raise ValueError("discard applies to the entity graph")
        # discard() itself decides whether trust could honestly be restored (the audited
        # quarantine target may have been re-quarantined since); say which happened
        # rather than letting the UI assume the best case.
        result = await module.discard(db, cfg, row["revision_id"])
        note = ("facts return on the restored revision" if result.get("restored")
                else "trust was not restored; facts on the earlier revision stay withheld")
        return {"status": "discarded", **result, "note": note}

    raise ValueError(f"unknown repair action {action!r}")


async def _finish(db, request_id: str, result: dict) -> None:
    await db.execute(
        """UPDATE repair_request SET state='done', result=%s, error=NULL, category=NULL,
                  retry_at=NULL, started_at=NULL, updated_at=now() WHERE id=%s""",
        (Jsonb(result), request_id),
    )


async def _fail(db, row: dict, exc: BaseException) -> None:
    category = failure_category(exc)
    attempts = row["attempts"] + 1
    delay = retry_delay_minutes(attempts) if category in TRANSIENT_CATEGORIES else None
    state = "pending" if delay else "failed"
    await db.execute(
        """UPDATE repair_request SET state=%s, error=%s, category=%s,
                  retry_at = CASE WHEN %s::int IS NULL THEN NULL
                                  ELSE now() + (%s::int * interval '1 minute') END,
                  started_at=NULL, updated_at=now() WHERE id=%s""",
        (state, f"{type(exc).__name__}: {exc}"[:2000], category, delay, delay, row["id"]),
    )
    run_id=(row.get('params') or {}).get('run_id')
    if run_id and row.get('action') in {'reextract','reextract_apply'}:
        terminal=('applying' if row.get('action')=='reextract_apply' else 'pending') if state=='pending' else 'failed'
        await db.execute("UPDATE chapter_knowledge_run SET state=%s,error=%s,updated_at=now() WHERE id=%s",
                         (terminal,f"{type(exc).__name__}: {exc}"[:2000],run_id))
        if terminal=='failed':
            await db.execute('''INSERT INTO chapter_knowledge_activity
                (run_id,novel_id,chapter_index,item_kind,item_key,phase,payload,idempotency_key)
                VALUES(%s,%s,%s,'run','run','rejected',%s,'run:rejected')
                ON CONFLICT(run_id,idempotency_key) DO NOTHING''',
                (run_id,row['novel_id'],row['chapter_index'],Jsonb({'error':str(exc)[:500]})))


async def refresh_reports(db, cfg, novel_id: str | None = None) -> str | None:
    """Freeze a review report for a rebuild that has just finished every chapter.

    ``preview`` is a write -- it stores the report into ``review`` guarded by the
    revision's version -- so a read-only HTTP handler cannot call it.  Taking it here
    means the browser has a report to review the moment a rebuild completes, and
    ``resume`` clearing ``review`` on every published chapter means a report can never
    outlive the state it described.
    """
    from pipeline import event_rebuild, graph_rebuild

    for track, module, revision_table, job_table in (
        ("graph", graph_rebuild, "graph_revision", "graph_job"),
        ("events", event_rebuild, "event_revision", "event_job"),
    ):
        cursor = await db.execute(
            f"""SELECT r.id::text FROM {revision_table} r
                 WHERE r.state = 'staging' AND r.review IS NULL
                   AND (%s::uuid IS NULL OR r.novel_id = %s::uuid)
                   AND EXISTS (SELECT 1 FROM {job_table} j WHERE j.revision_id = r.id)
                   AND NOT EXISTS (SELECT 1 FROM {job_table} j
                                    WHERE j.revision_id = r.id AND j.state <> 'done')
                 ORDER BY r.created_at LIMIT 1""",
            (novel_id, novel_id),
        )
        row = await cursor.fetchone()
        if not row:
            continue
        await module.preview(db, cfg, row[0])

        # preview's write is guarded by the revision's version, so it can legitimately
        # affect no rows. If review is still NULL the same revision is selected again next
        # tick -- and because a truthy return makes _drain_background stop for this tick,
        # reporting success here would starve chapter enrichment permanently on the most
        # expensive path there is. Say it did nothing instead, and let the rest run.
        stored = await (await db.execute(
            f"SELECT review IS NOT NULL FROM {revision_table} WHERE id=%s", (row[0],)
        )).fetchone()
        if not stored or not stored[0]:
            print(json.dumps(dict(repair="report", track=track, revision=row[0],
                                  state="not_frozen")), flush=True)
            return None
        return f"{track}:{row[0]}"
    return None


async def _next_staging_revision(db, revision_table: str, job_table: str,
                                 novel_id: str | None) -> str | None:
    """The newest staging revision per novel that has a chapter ready to run.

    Newest only: preparing again supersedes an earlier attempt, and draining an abandoned
    one would spend the model on a rebuild nobody is looking at -- the panel reports the
    newest as the replacement, so that is the one that must advance.

    The same fencing rule as the active drain: only the EARLIEST unfinished chapter may
    move, so a terminal failure holds the rest of the revision rather than letting later
    chapters publish identity state built on a gap.

    A revision blocked on a TRANSIENT_CATEGORIES cause (the local Ollama endpoint was
    down, or a call timed out) is still eligible once BLOCKED_RETRY_MINUTES has passed
    since it was blocked. resume()'s preamble re-checks reachability before spending any
    model call, so retrying costs nothing when the cause hasn't actually cleared -- it
    just re-blocks with a fresh blocked_at. Without this, a transient outage (e.g. an SSH
    tunnel to a remote Ollama flapping) left every staging revision stuck forever: nothing
    else ever clears blocked_at, and the repair panel has no "retry" action for a blocked
    revision, only for individual failed chapters.
    """
    cursor = await db.execute(
        f"""WITH newest AS (
              SELECT DISTINCT ON (r.novel_id) r.id, r.novel_id, r.created_at
               FROM {revision_table} r
               WHERE r.state = 'staging'
                 AND (r.blocked_at IS NULL
                      OR (r.blocked_category = ANY(%s)
                          AND r.blocked_at <= now() - (%s * interval '1 minute')))
                 AND (%s::uuid IS NULL OR r.novel_id = %s::uuid)
               ORDER BY r.novel_id, r.created_at DESC
            )
            SELECT n.id::text FROM newest n
              JOIN LATERAL (SELECT state, attempts, retry_at FROM {job_table}
                             WHERE revision_id = n.id AND state <> 'done'
                             ORDER BY chapter_index LIMIT 1) j ON true
             WHERE j.state IN ('pending', 'processing')
                OR (j.state = 'failed' AND j.attempts <= 3 AND j.retry_at <= now())
             ORDER BY n.created_at LIMIT 1""",
        (list(TRANSIENT_CATEGORIES), BLOCKED_RETRY_MINUTES, novel_id, novel_id),
    )
    row = await cursor.fetchone()
    return row[0] if row else None


async def drain_staging(cfg: Config, novel_id: str | None = None) -> str | None:
    """Advance one chapter of a prepared rebuild.

    Without this, pressing "Start fresh rebuild" quarantines the graph, snapshots the
    chapters, writes the job rows -- and then nothing ever runs them. graph_rebuild's and
    event_rebuild's own drains only advance a revision that is already ``state='active'
    AND trusted``, because their job is to keep an ACTIVATED graph up to date as new
    chapters arrive. A revision that prepare just created is ``staging`` and untrusted, so
    it matched neither, and resume stayed a command someone had to type at a shell.

    One chapter per tick, like every other background drain, so a rebuild can never hold
    the worker away from a chapter a reader is waiting to read.
    """
    from pipeline import event_rebuild, graph_rebuild

    async with await psycopg.AsyncConnection.connect(cfg.database_url, autocommit=True) as db:
        for track, module, revision_table, job_table in (
            ("graph", graph_rebuild, "graph_revision", "graph_job"),
            ("events", event_rebuild, "event_revision", "event_job"),
        ):
            rid = await _next_staging_revision(db, revision_table, job_table, novel_id)
            if not rid:
                continue
            # resume takes its own per-revision advisory lock, re-checks the model pin and
            # the saved prose against the snapshot, and records a failure with its safe
            # class before re-raising. The worker logs and keeps going.
            await module.resume(db, cfg, rid, limit=1)
            return f"{track}:{rid}"
    return None


async def drain_requests(cfg: Config, novel_id: str | None = None) -> str | None:
    """Run at most one repair action, then at most one report refresh.

    One action per tick on purpose: prepare quarantines a book and switch performs a
    cutover, and doing two of those back to back without letting the worker re-check the
    queue would let repair starve reader-critical work.
    """
    async with await psycopg.AsyncConnection.connect(cfg.database_url, autocommit=True) as db:
        await _reclaim_abandoned(db)
        row = await _claim(db, novel_id)
        if row is not None:
            # Serialize execution per novel and track for the whole of _run, the way
            # graph_rebuild.resume guards a revision.  The partial unique index stops two
            # requests being in flight, but a stale-claim sweep can retire a row while its
            # action is still running; without this lock that would allow two prepares, or
            # a prepare racing a cutover, on the same book.
            key = f"repair:{row['novel_id']}:{row['track']}"
            locked = (await (await db.execute(
                'SELECT pg_try_advisory_lock(hashtextextended(%s,0))', (key,))).fetchone())[0]
            if not locked:
                await db.execute(
                    """UPDATE repair_request SET state='pending', attempts=attempts-1,
                              started_at=NULL, updated_at=now() WHERE id=%s""",
                    (row["id"],),
                )
                return None
            try:
                result = await _run(db, cfg, row)
            except AdmissionRejected:
                # Backpressure, not failure: the model is busy with reader-facing work.
                # Return the request to the queue without consuming an attempt.
                await db.execute(
                    """UPDATE repair_request SET state='pending', attempts=attempts-1,
                              started_at=NULL, updated_at=now() WHERE id=%s""",
                    (row["id"],),
                )
                return None
            except Exception as exc:  # noqa: BLE001 - the row records why, and it retries
                await _fail(db, row, exc)
                print(json.dumps(dict(repair=row["id"], action=row["action"],
                                      state="failed", category=failure_category(exc))),
                      flush=True)
                return f"{row['action']}:failed"
            finally:
                await db.execute(
                    'SELECT pg_advisory_unlock(hashtextextended(%s,0))', (key,))
            await _finish(db, row["id"], result)
            print(json.dumps(dict(repair=row["id"], action=row["action"], state="done")),
                  flush=True)
            return f"{row['action']}:done"

        return await refresh_reports(db, cfg, novel_id)
