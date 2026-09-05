"""Execute knowledge-repair intents recorded by the API (migration 0043).

The repair verbs already exist and are carefully defended: ``prepare`` quarantines and
snapshots under a lock, ``resume`` rebuilds a chapter at a time under a model pin,
``preview`` freezes a report, ``record_review`` derives its metrics from per-item
assessments joined against actually-stored bindings, and ``switch`` is the only cutover.
Until now they were reachable only from argparse, so repairing a book meant six commands
at a shell.

This module is the bridge, and it is deliberately thin.  It claims a ``repair_request``
row and calls those same functions.  It re-implements no gate, lowers no threshold, and
skips no check: ``qualified`` still decides what may be activated, and a review submitted
from a browser goes through exactly the code a review submitted from a file does.

It runs on the worker's idle tick, below reader-critical translation (§0), because a
rebuild must never delay a chapter someone is waiting to read.
"""
from __future__ import annotations

import json

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from pipeline.config import Config
from pipeline.failures import ABANDONED, failure_category
from pipeline.llm.provider import AdmissionRejected

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
                          revision_id::text AS revision_id, params, attempts
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
            revision = await module.prepare(db, cfg, row["novel_id"], model.strip())
        else:
            provider = params.get("provider", "ollama")
            if provider not in event_rebuild.EXTRACTION_PROVIDERS:
                raise ValueError(f"unknown extraction provider {provider!r}")
            revision = await module.prepare(db, cfg, row["novel_id"], model.strip(),
                                            params.get("schema"), provider=provider)
        return {"revision": str(revision)}

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
    """
    cursor = await db.execute(
        f"""WITH newest AS (
              SELECT DISTINCT ON (r.novel_id) r.id, r.novel_id, r.created_at
                FROM {revision_table} r
               WHERE r.state = 'staging'
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
        (novel_id, novel_id),
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
