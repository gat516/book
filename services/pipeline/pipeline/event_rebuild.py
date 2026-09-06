"""Review-gated chapter-event revisions, independent of the entity graph (§0).

Commands mirror graph rebuilds, but preparing a revision never quarantines the active
entity graph or a previously active event revision.  Activation is the only cutover.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from pipeline.config import Config
from pipeline.evidence import digest
from pipeline.failures import clear_blocked, failure_category, record_blocked
from pipeline.events import (
    DEFAULT_EVENT_SCHEMA, EVENT_PROMPT_VERSION, EXTRACTION_PROVIDERS, EventEngine,
)
from pipeline.graph_rebuild import local_model, model_drift, objects, read_object
from pipeline.llm.provider import AdmissionRejected
from pipeline.provider_config import load_provider_config, load_provider_credential


async def revision(db, rid: str, *, lock: bool = False) -> dict:
    async with db.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM event_revision WHERE id=%s" + (" FOR UPDATE" if lock else ""),
            (rid,),
        )
        row = await cur.fetchone()
    if not row:
        raise ValueError("event revision not found")
    row["id"], row["novel_id"] = str(row["id"]), str(row["novel_id"])
    return row


async def extraction_model(cfg, provider: str, name: str) -> dict:
    """Pin the model an event revision is bound to, per provider.

    Ollama keeps the strong form: the model must be installed locally, and the recorded
    digest plus generation-affecting runtime settings let ``resume`` refuse to continue a
    revision whose weights changed underneath it.

    A hosted provider cannot offer that -- there is no digest to read, and a vendor may
    change what serves a stable model name.  Rather than fake an identity that implies a
    guarantee we do not have, record only provider and name, and lean on the two checks
    that still hold: EventEngine rejects a completion whose served provider/model is not
    the pinned pair, and served_model is part of event_completion's primary key, so a
    rename can never silently reuse another model's cached responses.  See migration
    0040 and docs/event-extraction-pilot-metrics.md.
    """
    if provider == "ollama":
        return await local_model(cfg, name)
    if provider not in EXTRACTION_PROVIDERS:
        raise ValueError(f"unsupported event extraction provider: {provider}")
    return dict(provider=provider, name=name, identity={})


async def provider_connection(db, cfg, novel: str, provider: str) -> dict:
    """Resolve secrets for the provider pinned by an event revision.

    Revisions pin provider+model but never credentials. Credentials remain rotatable and
    come from the same per-book-over-account hierarchy as the ordinary pipeline (§5.4).
    If the book has since switched providers, only the account credential for the pinned
    provider is eligible; silently borrowing another provider's secret would cross trust
    boundaries.
    """
    if provider == "ollama":
        return {}
    book = await load_provider_config(db, novel)
    account_base_url, account_api_key = await load_provider_credential(db, provider)
    book_matches = book is not None and book.provider == provider
    base_url = (book.base_url if book_matches else None) or account_base_url
    api_key = (book.api_key if book_matches else None) or account_api_key
    if provider == "gemini":
        api_key = api_key or cfg.gemini_api_key or None
        base_url = base_url or cfg.gemini_base_url
        if not api_key:
            raise RuntimeError("Gemini event extraction has no configured API key")
    return {"base_url": base_url, "api_key": api_key}


def qualified(metrics: dict) -> bool:
    return (
        metrics.get("reviewed_expected_events", 0) >= 30
        and metrics.get("reviewed_chapters", 0) >= 5
        and metrics.get("event_precision", 0) >= 0.95
        and metrics.get("plot_event_recall", 0) >= 0.85
        and metrics.get("argument_role_f1", 0) >= 0.85
        and metrics.get("completion_status_accuracy", 0) >= 0.95
        and metrics.get("critical_false_completions", 1) == 0
        and metrics.get("evidence_valid") is True
        and metrics.get("reviewed") is True
        and metrics.get("publication_review_complete") is True
    )


async def prepare(db, cfg, novel: str, model: str, schema: dict | None = None,
                  provider: str = "ollama") -> str:
    identity = await extraction_model(cfg, provider, model)
    client = objects(cfg)
    if not await (await db.execute("SELECT 1 FROM novel WHERE id=%s", (novel,))).fetchone():
        raise ValueError("novel not found")
    # Fail before creating a permanently blocked staging revision when a hosted provider
    # has no usable credentials. Secrets are resolved but never copied into the snapshot.
    await provider_connection(db, cfg, novel, provider)
    rows = await (await db.execute(
        """SELECT chapter_index,raw_uri,translated_uri,raw_hash FROM chapter
             WHERE novel_id=%s AND translation_ready ORDER BY chapter_index""",
        (novel,),
    )).fetchall()
    chapters = []
    for index, raw_uri, translated_uri, raw_hash in rows:
        event_uri = translated_uri or raw_uri
        text = await asyncio.to_thread(read_object, client, cfg, event_uri)
        chapters.append(dict(chapter=index, raw_uri=raw_uri, translated_uri=translated_uri,
                             event_uri=event_uri, raw_hash=raw_hash, source_hash=digest(text)))
    snapshot = {"chapters": chapters}
    async with db.transaction():
        rid = (await (await db.execute(
            """INSERT INTO event_revision
               (novel_id,snapshot,event_schema,model,prompt_version)
               VALUES(%s,%s,%s,%s,%s) RETURNING id""",
            (novel, Jsonb(snapshot), Jsonb(schema or DEFAULT_EVENT_SCHEMA),
             Jsonb(identity), EVENT_PROMPT_VERSION),
        )).fetchone())[0]
        for chapter in chapters:
            await db.execute(
                """INSERT INTO event_job
                   (revision_id,chapter_index,input_hash,model_identity,generation)
                   VALUES(%s,%s,%s,%s,1)""",
                (rid, chapter["chapter"], digest(chapter), digest(identity)),
            )
        await db.execute(
            "INSERT INTO event_audit(novel_id,revision_id,action,detail) VALUES(%s,%s,'prepare',%s)",
            (novel, rid, Jsonb({"chapters": len(chapters)})),
        )
    return str(rid)


# Past this many chapters failing back to back, the fault is the model or the
# connection rather than the prose, and continuing just marks everything failed.
MAX_CONSECUTIVE_CHAPTER_FAILURES = 3


async def record_job_failure(db, rid: str, index: int, exc: BaseException) -> int | None:
    """Mark one chapter failed with a safe class and its next retry time.

    Its own function for the same reason as graph_rebuild's: an error path is the worst
    place to keep an SQL statement no test can execute. The consecutive-failure circuit
    breaker and the stderr log line stay with the caller, which is where the decision to
    keep going or stop belongs.
    """
    attempts = (await (await db.execute(
        "SELECT attempts FROM event_job WHERE revision_id=%s AND chapter_index=%s",
        (rid, index),
    )).fetchone())[0]
    delay = retry_delay_minutes(attempts)
    await db.execute(
        """UPDATE event_job SET state='failed',error=%s,category=%s,
           retry_at=CASE WHEN %s::int IS NULL THEN NULL
                         ELSE now()+(%s::int*interval '1 minute') END,
           updated_at=now() WHERE revision_id=%s AND chapter_index=%s""",
        ((type(exc).__name__ + ": " + str(exc))[:2000], failure_category(exc),
         delay, delay, rid, index),
    )
    return delay


def retry_delay_minutes(attempt: int) -> int | None:
    return {1: 5, 2: 15, 3: 45}.get(attempt)


async def resume(db, cfg, rid: str, *, limit: int | None = None, chapter: int | None = None) -> None:
    locked = (await (await db.execute(
        "SELECT pg_try_advisory_lock(hashtextextended(%s,0))", ("event:" + rid,)
    )).fetchone())[0]
    if not locked:
        raise RuntimeError("another worker is already enriching this event revision")
    engine = None
    try:
        # Preamble: fails for reasons that belong to the run rather than to any chapter,
        # and used to die without ever reaching the per-chapter recorder.
        try:
            r = await revision(db, rid)
            if r["state"] == "archived":
                raise ValueError("archived event revision cannot be rebuilt")
            live = await extraction_model(cfg, r["model"]["provider"], r["model"]["name"])
            if live != r["model"]:
                raise ValueError(
                    "pinned model or inference configuration changed; create a new event "
                    f"revision ({model_drift(r['model'], live)})"
                )
            connection = await provider_connection(
                db, cfg, r["novel_id"], r["model"]["provider"])
            engine = EventEngine(db, cfg, r, provider_connection=connection)
            target = (await (await db.execute(
                "SELECT target_lang FROM novel WHERE id=%s", (r["novel_id"],)
            )).fetchone())[0]
            jobs = await (await db.execute(
                "SELECT chapter_index FROM event_job WHERE revision_id=%s AND state<>'done' "
                "AND (%s::int IS NULL OR chapter_index=%s) ORDER BY chapter_index", (rid, chapter, chapter)
            )).fetchall()
            selected = jobs[:limit] if limit else jobs
            client = objects(cfg)
        except Exception as exc:
            await record_blocked(db, "event_revision", rid, exc)
            raise
        await clear_blocked(db, "event_revision", rid)
        # A chapter that fails is recorded and skipped rather than ending the run: the
        # event_job state='failed'/retry_at machinery exists precisely so one bad chapter
        # does not cost the other twenty. Consecutive failures are different -- they mean
        # the model or the connection is gone, not that one chapter is awkward -- so the
        # run still stops rather than marking every remaining chapter failed in turn.
        consecutive_failures = 0
        for (index,) in selected:
            chapter = next(c for c in r["snapshot"]["chapters"] if c["chapter"] == index)
            source = await asyncio.to_thread(
                read_object, client, cfg, chapter.get("event_uri", chapter["raw_uri"])
            )
            if digest(source) != chapter["source_hash"]:
                raise ValueError("saved source changed since event snapshot")
            await db.execute(
                """UPDATE event_job SET state='processing',attempts=attempts+1,error=NULL,
                   category=NULL,retry_at=NULL,generation=%s,updated_at=now()
                   WHERE revision_id=%s AND chapter_index=%s""",
                (r["generation"], rid, index),
            )
            try:
                output = await engine.extract(r["novel_id"], index, source, target)
                live = await extraction_model(
                    cfg, r["model"]["provider"], r["model"]["name"]
                )
                if live != r["model"]:
                    raise ValueError(
                        "model or inference configuration changed during event extraction "
                        f"({model_drift(r['model'], live)})"
                    )
                async with db.transaction():
                    current = await revision(db, rid, lock=True)
                    if current["generation"] != r["generation"] or current["state"] == "archived":
                        raise RuntimeError("event worker fenced by revision change")
                    saved = await (await db.execute(
                        "SELECT raw_uri,translated_uri,raw_hash FROM chapter WHERE novel_id=%s AND chapter_index=%s FOR SHARE",
                        (r["novel_id"], index),
                    )).fetchone()
                    if saved != (chapter["raw_uri"], chapter.get("translated_uri"), chapter["raw_hash"]):
                        raise ValueError("chapter input changed during event extraction")
                    await db.execute(
                        "SELECT set_config('app.event_revision',%s,true),"
                        "set_config('app.event_generation',%s,true)",
                        (rid, str(r["generation"])),
                    )
                    await engine.publish(r["novel_id"], index, source, output)
                    await db.execute(
                        """UPDATE event_job SET state='done',output=%s,retry_at=NULL,updated_at=now()
                           WHERE revision_id=%s AND chapter_index=%s""",
                        (Jsonb(output), rid, index),
                    )
                    await db.execute(
                        "UPDATE event_revision SET version=version+1,review=NULL WHERE id=%s", (rid,)
                    )
                consecutive_failures = 0
            except AdmissionRejected:
                await db.execute(
                    "UPDATE event_job SET state='pending',error=NULL,category=NULL,retry_at=NULL,updated_at=now() "
                    "WHERE revision_id=%s AND chapter_index=%s", (rid, index),
                )
                raise
            except Exception as exc:
                await record_job_failure(db, rid, index, exc)
                consecutive_failures += 1
                print(json.dumps(dict(revision=rid, chapter=index, state="failed",
                                      consecutive_failures=consecutive_failures,
                                      error=f"{type(exc).__name__}: {exc}"[:500])),
                      file=sys.stderr, flush=True)
                if consecutive_failures >= MAX_CONSECUTIVE_CHAPTER_FAILURES:
                    raise
    finally:
        if engine:
            await engine.close()
        await db.execute("SELECT pg_advisory_unlock(hashtextextended(%s,0))", ("event:" + rid,))


async def preview(db, cfg, rid: str) -> dict:
    r = await revision(db, rid)
    client = objects(cfg)
    sources = {}
    unchanged = True
    for chapter in r["snapshot"].get("chapters", []):
        source = await asyncio.to_thread(
            read_object, client, cfg, chapter.get("event_uri", chapter["raw_uri"])
        )
        sources[chapter["chapter"]] = source
        saved = await (await db.execute(
            "SELECT raw_uri,translated_uri,raw_hash FROM chapter WHERE novel_id=%s AND chapter_index=%s",
            (r["novel_id"], chapter["chapter"]),
        )).fetchone()
        unchanged = unchanged and digest(source) == chapter["source_hash"] and saved == (
            chapter["raw_uri"], chapter.get("translated_uri"), chapter["raw_hash"]
        )
    jobs = await (await db.execute(
        "SELECT chapter_index,state,error,output FROM event_job WHERE revision_id=%s ORDER BY chapter_index",
        (rid,),
    )).fetchall()
    rows = await (await db.execute(
        """SELECT e.id::text,e.chapter_index,e.event_type,e.action,e.status,e.summary,e.result,
                  v.quote,v.source_hash,v.char_start,v.char_end
             FROM chapter_event e JOIN event_evidence v ON v.id=e.evidence_id
            WHERE e.revision_id=%s ORDER BY e.chapter_index,e.id""", (rid,)
    )).fetchall()
    events = []
    for row in rows:
        event_id = row[0]
        args = await (await db.execute(
            """SELECT role,surface,entity_id::text FROM chapter_event_argument
                 WHERE event_id=%s ORDER BY ordinal""", (event_id,)
        )).fetchall()
        events.append(dict(
            zip(["id", "chapter", "event_type", "action", "status", "summary", "result",
                 "quote", "source_hash", "start", "end"], row),
            arguments=[{"role": role, "surface": surface, "entity_id": entity_id}
                       for role, surface, entity_id in args],
        ))
    evidence_valid = all(
        event["chapter"] in sources
        and digest(sources[event["chapter"]]) == event["source_hash"]
        and sources[event["chapter"]][event["start"]:event["end"]] == event["quote"]
        for event in events
    )
    report = {
        "revision": rid, "generation": r["generation"], "version": r["version"],
        "model": r["model"], "prompt_version": r["prompt_version"],
        "event_schema": r["event_schema"], "evaluation": r["evaluation"],
        "saved_source_unchanged": unchanged, "evidence_valid": evidence_valid,
        "completed": sum(job[1] == "done" for job in jobs), "total_jobs": len(jobs),
        "events": events,
        "failures": [{"chapter": j[0], "state": j[1], "error": j[2]}
                     for j in jobs if j[1] != "done"],
        # Already stored in event_job.output, just never surfaced. A report that shows only
        # what was ACCEPTED cannot answer the question a review actually asks -- whether the
        # gate threw away something true -- and the graph path has surfaced its rejections
        # all along (graph_rebuild.preview). Reasons are the diagnostic: across this repo's
        # revisions, 41 of 53 rejections are missing roles or invalid arguments.
        "rejected": [dict(chapter=j[0], **x)
                     for j in jobs if j[3] for x in j[3].get("rejected", [])],
    }
    report["activation_eligible"] = (
        r["prompt_version"] == EVENT_PROMPT_VERSION and qualified(r["evaluation"])
        and unchanged and evidence_valid and bool(jobs) and all(j[1] == "done" for j in jobs)
    )
    report["review_hash"] = digest(report)
    await db.execute(
        "UPDATE event_revision SET review=%s WHERE id=%s AND version=%s",
        (Jsonb(report), rid, r["version"]),
    )
    return report


async def record_review(db, cfg, rid: str, document: dict) -> dict:
    report = await preview(db, cfg, rid)
    if (document.get("review_hash") != report["review_hash"]
            or not document.get("reviewer") or document.get("approved") is not True):
        raise ValueError("review must approve the current event report hash and name its reviewer")
    actual = {event["id"]: event for event in report["events"]}
    assessments = document.get("events", [])
    if len({a.get("id") for a in assessments}) != len(assessments) or {
        a.get("id") for a in assessments
    } != set(actual):
        raise ValueError("review must assess every published event exactly once")
    required = {"correct", "arguments_correct", "status_correct"}
    if any(any(type(a.get(field)) is not bool for field in required) for a in assessments):
        raise ValueError("event assessments require explicit boolean judgments")
    expected = document.get("expected_events", [])
    assessment_by_id = {a["id"]: a for a in assessments}
    matched = [e for e in expected if e.get("matched_event_id") in actual
               and assessment_by_id[e["matched_event_id"]]["correct"]]
    reviewed_chapters = {e.get("chapter") for e in expected if isinstance(e.get("chapter"), int)}
    correct = [a for a in assessments if a["correct"]]
    argument_tp = argument_fp = argument_fn = 0
    matched_ids = set()
    for item in expected:
        expected_arguments = item.get("arguments")
        if not isinstance(expected_arguments, list) or any(
            not isinstance(a, dict) or not isinstance(a.get("role"), str)
            or not isinstance(a.get("surface"), str) for a in expected_arguments
        ):
            raise ValueError("every expected event requires literal role/surface arguments")
        expected_set = {(a["role"], a["surface"]) for a in expected_arguments}
        matched_id = item.get("matched_event_id")
        if matched_id in actual and assessment_by_id[matched_id]["correct"]:
            matched_ids.add(matched_id)
            actual_set = {(a["role"], a["surface"])
                          for a in actual[matched_id]["arguments"]}
            argument_tp += len(expected_set & actual_set)
            argument_fp += len(actual_set - expected_set)
            argument_fn += len(expected_set - actual_set)
        else:
            argument_fn += len(expected_set)
    for event_id, event in actual.items():
        if event_id not in matched_ids and assessment_by_id[event_id]["correct"]:
            argument_fp += len({(a["role"], a["surface"])
                                for a in event["arguments"]})
    argument_denominator = 2 * argument_tp + argument_fp + argument_fn
    metrics = {
        "reviewed": True, "publication_review_complete": True,
        "reviewed_expected_events": len(expected), "reviewed_chapters": len(reviewed_chapters),
        "event_precision": len(correct) / len(assessments) if assessments else 0,
        "plot_event_recall": len(matched) / len(expected) if expected else 0,
        "argument_role_f1": (2 * argument_tp / argument_denominator
                             if argument_denominator else 0),
        "completion_status_accuracy": sum(a["status_correct"] for a in assessments) / len(assessments)
                                      if assessments else 0,
        "critical_false_completions": sum(
            bool(a.get("critical")) and not a["correct"]
            and actual[a["id"]]["status"] == "completed" for a in assessments
        ),
        "evidence_valid": report["evidence_valid"],
    }
    async with db.transaction():
        current = await revision(db, rid, lock=True)
        if current["version"] != report["version"] or current["state"] != "staging":
            raise ValueError("event revision changed while reviewing")
        await db.execute(
            "UPDATE event_revision SET evaluation=%s,review=NULL WHERE id=%s",
            (Jsonb(metrics), rid),
        )
        await db.execute(
            "INSERT INTO event_audit(novel_id,revision_id,action,detail) VALUES(%s,%s,'review',%s)",
            (current["novel_id"], rid, Jsonb(document)),
        )
    return await preview(db, cfg, rid)


async def switch(db, cfg, rid: str, review_hash: str | None = None, *, rollback: bool = False) -> None:
    report = None if rollback else await preview(db, cfg, rid)
    async with db.transaction():
        target = await revision(db, rid, lock=True)
        current_row = await (await db.execute(
            "SELECT active_event_revision FROM novel WHERE id=%s FOR UPDATE", (target["novel_id"],)
        )).fetchone()
        old = str(current_row[0]) if current_row and current_row[0] else None
        if rollback:
            if target["state"] != "archived":
                raise ValueError("rollback target must be an archived event revision")
        elif (target["state"] != "staging" or not report["activation_eligible"]
              or report["review_hash"] != review_hash or target["review"] != report):
            raise ValueError("activation requires the current qualifying event review")
        if old:
            await db.execute(
                "UPDATE event_revision SET state='archived',generation=generation+1,version=version+1 "
                "WHERE id=%s", (old,),
            )
        await db.execute(
            """UPDATE event_revision SET state='active',trusted=true,
               generation=generation+1,version=version+1 WHERE id=%s""", (rid,),
        )
        await db.execute(
            "UPDATE novel SET active_event_revision=%s WHERE id=%s", (rid, target["novel_id"])
        )
        await db.execute(
            "INSERT INTO event_audit(novel_id,revision_id,action,detail) VALUES(%s,%s,%s,%s)",
            (target["novel_id"], rid, "rollback" if rollback else "activate",
             Jsonb({"previous": old, "review_hash": review_hash})),
        )


async def enqueue_completed(db, cfg, novel: str) -> None:
    row = await (await db.execute(
        """SELECT r.id::text FROM event_revision r JOIN novel n ON n.active_event_revision=r.id
             WHERE n.id=%s AND r.state='active' AND r.trusted""", (novel,)
    )).fetchone()
    if not row:
        return
    r = await revision(db, row[0])
    known = {c["chapter"] for c in r["snapshot"].get("chapters", [])}
    additions = []
    client = objects(cfg)
    rows = await (await db.execute(
        "SELECT chapter_index,raw_uri,translated_uri,raw_hash FROM chapter "
        "WHERE novel_id=%s AND translation_ready ORDER BY chapter_index", (novel,)
    )).fetchall()
    for index, raw_uri, translated_uri, raw_hash in rows:
        if index in known:
            continue
        event_uri = translated_uri or raw_uri
        text = await asyncio.to_thread(read_object, client, cfg, event_uri)
        additions.append(dict(chapter=index, raw_uri=raw_uri, translated_uri=translated_uri,
                              event_uri=event_uri, raw_hash=raw_hash, source_hash=digest(text)))
    if not additions:
        return
    async with db.transaction():
        current = await revision(db, r["id"], lock=True)
        if current["generation"] != r["generation"] or current["state"] != "active":
            return
        known = {c["chapter"] for c in current["snapshot"].get("chapters", [])}
        for chapter in additions:
            if chapter["chapter"] in known:
                continue
            current["snapshot"]["chapters"].append(chapter)
            await db.execute(
                """INSERT INTO event_job
                   (revision_id,chapter_index,input_hash,model_identity,generation)
                   VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                (r["id"], chapter["chapter"], digest(chapter), digest(r["model"]), r["generation"]),
            )
        await db.execute(
            "UPDATE event_revision SET snapshot=%s,version=version+1 WHERE id=%s",
            (Jsonb(current["snapshot"]), r["id"]),
        )
        await db.execute(
            "INSERT INTO event_audit(novel_id,revision_id,action,detail) "
            "VALUES(%s,%s,'append_chapters',%s)",
            (r["novel_id"], r["id"], Jsonb({"chapters": [c["chapter"] for c in additions]})),
        )


async def next_retryable_active_revision(db, novel_id: str | None = None) -> str | None:
    row = await (await db.execute(
        """SELECT r.id::text FROM event_revision r
             JOIN LATERAL (SELECT state,attempts,retry_at FROM event_job
                            WHERE revision_id=r.id AND state<>'done'
                            ORDER BY chapter_index LIMIT 1) j ON true
            WHERE (%s::uuid IS NULL OR r.novel_id=%s::uuid)
              AND r.state='active' AND r.trusted
              AND (j.state IN ('pending','processing') OR
                   (j.state='failed' AND j.attempts<=3 AND j.retry_at<=now()))
            ORDER BY r.created_at LIMIT 1""", (novel_id, novel_id)
    )).fetchone()
    return row[0] if row else None


async def drain_active(cfg, novel_id: str | None = None) -> None:
    async with await psycopg.AsyncConnection.connect(cfg.database_url, autocommit=True) as db:
        rid = await next_retryable_active_revision(db, novel_id)
        if rid:
            await resume(db, cfg, rid, limit=1)


async def main(args) -> None:
    cfg = Config.load()
    async with await psycopg.AsyncConnection.connect(cfg.database_url, autocommit=True) as db:
        if args.command == "prepare":
            schema = json.loads(Path(args.schema).read_text()) if args.schema else None
            result = {"revision": await prepare(db, cfg, args.novel, args.model, schema,
                                                provider=args.provider),
                      "status": "staging; active graph and events unchanged"}
        elif args.command == "resume":
            await resume(db, cfg, args.revision, limit=args.limit, chapter=args.chapter)
            result = {"status": "resume finished"}
        elif args.command == "preview":
            result = await preview(db, cfg, args.revision)
        elif args.command == "review":
            result = await record_review(
                db, cfg, args.revision, json.loads(Path(args.file).read_text())
            )
        else:
            await switch(db, cfg, args.revision, getattr(args, "review_hash", None),
                         rollback=args.command == "rollback")
            result = {"status": args.command, "revision": args.revision}
        rendered = json.dumps(result, ensure_ascii=False, indent=2, default=str)
        if getattr(args, "output", None):
            Path(args.output).write_text(rendered + "\n")
        print(rendered)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("prepare")
    command.add_argument("--novel", required=True)
    command.add_argument("--model", required=True)
    command.add_argument("--provider", default="ollama", choices=EXTRACTION_PROVIDERS)
    command.add_argument("--schema")
    for name in ["resume", "preview", "review", "activate", "rollback"]:
        command = commands.add_parser(name)
        command.add_argument("--revision", required=True)
        if name == "resume":
            command.add_argument("--limit", type=int)
            command.add_argument("--chapter", type=int)
        if name == "preview":
            command.add_argument("--output")
        if name == "review":
            command.add_argument("--file", required=True)
        if name == "activate":
            command.add_argument("--review-hash", required=True)
    asyncio.run(main(parser.parse_args()))
