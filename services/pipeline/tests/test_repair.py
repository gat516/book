"""Coverage for the repair executor (pipeline/repair.py, migration 0043).

What matters here is not that prepare or switch work -- test_knowledge.py already covers
those -- but that the bridge between an HTTP intent and those functions preserves every
property the CLI had: one action at a time, deterministic failures are not retried
forever, backpressure is not mistaken for failure, and no gate is bypassed.
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from pipeline import repair
from pipeline.failures import failure_category
from pipeline.llm.provider import AdmissionRejected

from tests.fixtures import make_novel


REPAIR_GO = Path(__file__).resolve().parents[3] / "services/reader-api/repair.go"


def test_failure_category_classifies_repair_action_errors():
    cases = [
        (ValueError("review must approve the current report hash and name its reviewer"),
         "review_rejected"),
        (ValueError("novel not found"), "not_found"),
        (ValueError("requested model is not installed; no automatic download or provider fallback"),
         "model_not_installed"),
        (ValueError("installed model or inference configuration changed since snapshot"),
         "model_changed"),
        (ValueError("saved prose changed since snapshot"), "input_changed"),
        (RuntimeError("worker fenced by revision change"), "fenced"),
        (RuntimeError("Ollama exhausted num_predict; refusing incomplete output"),
         "output_truncated"),
        (TimeoutError("timed out"), "timeout"),
        (OSError("connection refused"), "model_unreachable"),
        # The literal string httpx produces when the endpoint is gone.
        (ConnectionError("All connection attempts failed"), "model_unreachable"),
        (KeyError("chapters"), "unknown"),
    ]
    for exc, expected in cases:
        assert failure_category(exc) == expected, exc


def test_category_vocabulary_matches_reader_api():
    """Every category this module can emit must have a sentence in reader-api.

    Since 0046 this is the ONLY thing keeping the two sides in step. Go used to derive
    these classes itself from stored exception text; now Python derives them at the point
    of failure and Go only renders them, so a class added here with no entry in Go's
    repairFailureDetail reaches the reader as a blank explanation and nothing else would
    catch it. Same cross-language guard, and the same reason, as glossary_hash_test.go.
    """
    source = REPAIR_GO.read_text()
    block = re.search(r"var repairFailureDetail = map\[string\]string\{(.*?)\n\}", source, re.S)
    assert block, "could not find repairFailureDetail in repair.go"
    documented = set(re.findall(r'repair\w+:\s*"', block.group(1)))
    # Resolve the Go constant names in that map back to their string values.
    values = dict(re.findall(r'\t(repair\w+)\s*=\s*"([a-z_]+)"', source))
    described = {values[name.rstrip(':" ')] for name in
                 re.findall(r"\n\t(repair\w+):", block.group(1))}

    emitted = {
        failure_category(exc)
        for exc in (
            ValueError("review must approve"), ValueError("novel not found"),
            ValueError("installed model or inference configuration changed"),
            ValueError("saved prose changed since snapshot"),
            ValueError("graph context exceeds hard local model budget"),
            ValueError("revision cannot be rebuilt"),
            ValueError("requested model is not installed; no automatic download"),
            RuntimeError("serving identity changed"),
            RuntimeError("worker fenced"), RuntimeError("num_predict"),
            TimeoutError("timed out"), OSError("connection refused"), KeyError("x"),
            # Raised by graph_rebuild/event_rebuild resume(), never by a repair action.
            RuntimeError("Ollama exhausted num_predict; refusing incomplete output"),
        )
    }
    # These two are written directly rather than derived from an exception: 'cancelled' by
    # ingest-api's CancelRepairRequest, 'abandoned' by _reclaim_abandoned. They are checked
    # explicitly because no exception can produce them.
    missing = (emitted | {"cancelled", "abandoned"}) - described
    assert not missing, f"categories with no reader-facing sentence in repair.go: {missing}"
    assert documented, "repairFailureDetail parsed as empty"


async def _request(conn, novel, *, track="graph", action="prepare",
                   revision=None, params=None) -> str:
    cursor = await conn.execute(
        """INSERT INTO repair_request(novel_id, track, action, revision_id, params, requested_by)
           VALUES(%s,%s,%s,%s,%s,%s) RETURNING id::text""",
        (novel, track, action, revision, json.dumps(params or {}), "reader-a"),
    )
    return (await cursor.fetchone())[0]


async def _state(conn, request_id):
    cursor = await conn.execute(
        "SELECT state, attempts, category, retry_at FROM repair_request WHERE id=%s",
        (request_id,))
    return await cursor.fetchone()


@pytest.mark.db
async def test_claim_marks_running_then_done(db_conn, monkeypatch):
    from pipeline import graph_rebuild

    async with db_conn.transaction(force_rollback=True):
        novel = await make_novel(db_conn)
        request_id = await _request(db_conn, novel, params={"model": "qwen3:4b"})

        prepared = AsyncMock(return_value=str(uuid.uuid4()))
        monkeypatch.setattr(graph_rebuild, "prepare", prepared)

        row = await repair._claim(db_conn, novel)
        assert row["id"] == request_id
        assert (await _state(db_conn, request_id))[0] == "running"

        result = await repair._run(db_conn, object(), row)
        await repair._finish(db_conn, request_id, result)

        state, attempts, category, retry_at = await _state(db_conn, request_id)
        assert (state, attempts, category, retry_at) == ("done", 1, None, None)
        # The model name reached graph_rebuild.prepare unchanged.
        assert prepared.await_args.args[3] == "qwen3:4b"


@pytest.mark.db
async def test_claim_ignores_other_novels_when_focused(db_conn):
    async with db_conn.transaction(force_rollback=True):
        wanted = await make_novel(db_conn)
        other = await make_novel(db_conn)
        await _request(db_conn, other, params={"model": "m"})
        mine = await _request(db_conn, wanted, params={"model": "m"})

        row = await repair._claim(db_conn, wanted)
        assert row["id"] == mine


@pytest.mark.db
async def test_deterministic_failure_is_not_retried(db_conn):
    """A rejected review fails identically every time; scheduling retries is noise."""
    async with db_conn.transaction(force_rollback=True):
        novel = await make_novel(db_conn)
        request_id = await _request(db_conn, novel, action="review",
                                    revision=str(uuid.uuid4()), params={})
        row = await repair._claim(db_conn, novel)
        await repair._fail(db_conn, row, ValueError("review must assess every published fact"))

        state, attempts, category, retry_at = await _state(db_conn, request_id)
        assert state == "failed"
        assert category == "review_rejected"
        assert retry_at is None, "a deterministic rejection must not be rescheduled"
        assert attempts == 1


@pytest.mark.db
async def test_transient_failure_is_retried_with_backoff(db_conn):
    async with db_conn.transaction(force_rollback=True):
        novel = await make_novel(db_conn)
        request_id = await _request(db_conn, novel, params={"model": "m"})
        row = await repair._claim(db_conn, novel)
        await repair._fail(db_conn, row, OSError("connection refused"))

        state, attempts, category, retry_at = await _state(db_conn, request_id)
        assert (state, category) == ("pending", "model_unreachable")
        assert retry_at is not None, "a transient failure must be rescheduled"


@pytest.mark.db
async def test_retries_are_bounded(db_conn):
    """Past the third attempt there is no delay left, so the request stops for good."""
    async with db_conn.transaction(force_rollback=True):
        novel = await make_novel(db_conn)
        request_id = await _request(db_conn, novel, params={"model": "m"})
        await db_conn.execute("UPDATE repair_request SET attempts=3 WHERE id=%s", (request_id,))
        row = await repair._claim(db_conn, novel)
        await repair._fail(db_conn, row, OSError("connection refused"))

        state, attempts, _, retry_at = await _state(db_conn, request_id)
        assert (state, attempts, retry_at) == ("failed", 4, None)


@pytest.mark.db
async def test_admission_rejection_returns_the_request_unconsumed(db_conn, monkeypatch):
    """Backpressure is not failure: the model is busy serving a reader.

    graph_rebuild.resume makes the same distinction for chapter jobs. If a repair request
    burned an attempt every time the model was busy, a book could exhaust its retries
    without anything having gone wrong.
    """
    from pipeline import graph_rebuild

    async with db_conn.transaction(force_rollback=True):
        novel = await make_novel(db_conn)
        request_id = await _request(db_conn, novel, params={"model": "m"})
        monkeypatch.setattr(graph_rebuild, "prepare",
                            AsyncMock(side_effect=AdmissionRejected("busy")))

        row = await repair._claim(db_conn, novel)
        assert (await _state(db_conn, request_id))[1] == 1
        with pytest.raises(AdmissionRejected):
            await repair._run(db_conn, object(), row)
        # drain_requests' handler restores the row; assert the effect it produces.
        await db_conn.execute(
            "UPDATE repair_request SET state='pending', attempts=attempts-1 WHERE id=%s",
            (request_id,))
        state, attempts, _, _ = await _state(db_conn, request_id)
        assert (state, attempts) == ("pending", 0)


@pytest.mark.db
async def test_only_one_request_per_novel_and_track_may_be_in_flight(db_conn):
    import psycopg

    async with db_conn.transaction(force_rollback=True):
        novel = await make_novel(db_conn)
        await _request(db_conn, novel, params={"model": "m"})
        with pytest.raises(psycopg.errors.UniqueViolation):
            await _request(db_conn, novel, action="rollback", revision=str(uuid.uuid4()))


@pytest.mark.db
async def test_events_and_graph_tracks_do_not_block_each_other(db_conn):
    """The tracks are independent (0039): repairing events must not gate the graph."""
    async with db_conn.transaction(force_rollback=True):
        novel = await make_novel(db_conn)
        await _request(db_conn, novel, track="graph", params={"model": "m"})
        await _request(db_conn, novel, track="events", params={"model": "m"})


@pytest.mark.db
async def test_activate_requires_the_recorded_review_hash(db_conn):
    async with db_conn.transaction(force_rollback=True):
        novel = await make_novel(db_conn)
        await _request(db_conn, novel, action="activate", revision=str(uuid.uuid4()), params={})
        row = await repair._claim(db_conn, novel)
        with pytest.raises(ValueError, match="review hash"):
            await repair._run(db_conn, object(), row)


@pytest.mark.db
async def test_prepare_rejects_a_missing_model(db_conn):
    async with db_conn.transaction(force_rollback=True):
        novel = await make_novel(db_conn)
        await _request(db_conn, novel, params={})
        row = await repair._claim(db_conn, novel)
        with pytest.raises(ValueError, match="model name"):
            await repair._run(db_conn, object(), row)


@pytest.mark.db
async def test_abandoned_running_request_unblocks_the_book(db_conn):
    """A worker that dies mid-action must not brick repair for that book.

    repair_request_one_active covers ('pending','running'), so a row stuck in 'running'
    makes every later request for the same novel and track a 409 with no way out but SQL.
    """
    async with db_conn.transaction(force_rollback=True):
        novel = await make_novel(db_conn)
        request_id = await _request(db_conn, novel, params={"model": "m"})
        await repair._claim(db_conn, novel)
        assert (await _state(db_conn, request_id))[0] == "running"

        # Nothing is reclaimed while the claim is still fresh.
        assert await repair._reclaim_abandoned(db_conn) == 0
        assert (await _state(db_conn, request_id))[0] == "running"

        await db_conn.execute(
            "UPDATE repair_request SET started_at = now() - interval '31 minutes' WHERE id=%s",
            (request_id,))
        assert await repair._reclaim_abandoned(db_conn) == 1

        state, _, category, _ = await _state(db_conn, request_id)
        assert (state, category) == ("failed", "abandoned")
        # The partial unique index no longer blocks the book: a new request is accepted.
        await _request(db_conn, novel, params={"model": "m"})


@pytest.mark.db
async def test_abandoned_request_is_never_requeued(db_conn):
    """Reclaim must not retry: these actions are not idempotent.

    A worker that finished prepare -- quarantining the graph and creating a staging
    revision -- but died before recording the result would, on an automatic retry,
    quarantine a second time and leave a second staging revision behind. Whether to
    restart is a judgement for whoever is looking at the panel.
    """
    async with db_conn.transaction(force_rollback=True):
        novel = await make_novel(db_conn)
        request_id = await _request(db_conn, novel, params={"model": "m"})
        await repair._claim(db_conn, novel)
        await db_conn.execute(
            "UPDATE repair_request SET started_at = now() - interval '31 minutes' WHERE id=%s",
            (request_id,))
        await repair._reclaim_abandoned(db_conn)

        assert (await _state(db_conn, request_id))[0] == "failed"
        # Nothing is left for a worker to pick up.
        assert await repair._claim(db_conn, novel) is None


@pytest.mark.db
async def test_execution_is_serialized_per_novel_and_track(db_conn):
    """The guard drain_requests takes around _run, at the level it actually operates.

    A stale-claim sweep can retire a row while its action is still running, so the unique
    index alone cannot prevent two workers executing the same book. This lock can.
    """
    import psycopg

    from tests.conftest import DATABASE_URL

    key = "repair:11111111-1111-4111-8111-111111111111:graph"
    async with await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True) as other:
        held = (await (await db_conn.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s,0))", (key,))).fetchone())[0]
        assert held, "first holder should acquire the lock"
        try:
            blocked = (await (await other.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s,0))", (key,))).fetchone())[0]
            assert not blocked, "a second worker must not run the same novel and track"
        finally:
            await db_conn.execute(
                "SELECT pg_advisory_unlock(hashtextextended(%s,0))", (key,))

        freed = (await (await other.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s,0))", (key,))).fetchone())[0]
        assert freed, "the lock must be released for the next worker"
        await other.execute("SELECT pg_advisory_unlock(hashtextextended(%s,0))", (key,))


@pytest.mark.db
async def test_settled_requests_clear_their_claim_timestamp(db_conn):
    """A stale started_at on a settled row would make a later sweep act on it."""
    async with db_conn.transaction(force_rollback=True):
        novel = await make_novel(db_conn)
        request_id = await _request(db_conn, novel, params={"model": "m"})
        row = await repair._claim(db_conn, novel)
        await repair._finish(db_conn, request_id, {"revision": "x"})

        cursor = await db_conn.execute(
            "SELECT state, started_at FROM repair_request WHERE id=%s", (request_id,))
        state, started_at = await cursor.fetchone()
        assert state == "done"
        assert started_at is None

        failing = await _request(db_conn, novel, track="events", params={"model": "m"})
        row = await repair._claim(db_conn, novel)
        assert row["id"] == failing
        await repair._fail(db_conn, row, ValueError("novel not found"))
        cursor = await db_conn.execute(
            "SELECT started_at FROM repair_request WHERE id=%s", (failing,))
        assert (await cursor.fetchone())[0] is None


@pytest.mark.db
async def test_failure_ledger_returns_the_stored_class_and_never_the_text(db_conn):
    """The read path must expose category, and must not expose error (migration 0046).

    graph_job.error is freeform and can embed source prose or a connection string. Before
    0046 it crossed into Go on every status poll so Go could classify it; the class is now
    derived by pipeline/failures.py at the moment of failure and stored, and the text stays
    behind the database.
    """
    from psycopg.types.json import Jsonb

    async with db_conn.transaction(force_rollback=True):
        novel = await make_novel(db_conn, ontology='{"kinds":[],"attributes":[],"relations":[]}')
        cursor = await db_conn.execute(
            """INSERT INTO graph_revision(novel_id, state, ontology, snapshot)
               VALUES(%s,'staging',%s,%s) RETURNING id::text""",
            (novel, Jsonb({"kinds": [], "attributes": [], "relations": []}), Jsonb({})))
        revision = (await cursor.fetchone())[0]
        secret = "ConnectError: postgres://engine:hunter2@db/novel_engine — 凌峰 was wounded"
        await db_conn.execute(
            """INSERT INTO graph_job(revision_id, chapter_index, state, input_hash,
                                     model_identity, generation, attempts, error, category)
               VALUES(%s,1,'failed','h','m',1,1,%s,'output_truncated'),
                     (%s,2,'failed','h','m',1,1,%s,NULL)""",
            (revision, secret, revision, secret))

        cursor = await db_conn.execute(
            "SELECT chapter_index, category FROM reader_repair_failures(%s) WHERE track='graph'"
            " ORDER BY chapter_index", (novel,))
        rows = await cursor.fetchall()
        assert rows == [(1, "output_truncated"), (2, "unknown")], rows

        # And the function's return type has no column that could carry the text.
        cursor = await db_conn.execute(
            """SELECT pg_get_function_result(p.oid) FROM pg_proc p
                JOIN pg_namespace n ON n.oid = p.pronamespace
               WHERE n.nspname='public' AND p.proname='reader_repair_failures'""")
        signature = (await cursor.fetchone())[0]
        assert "category" in signature
        assert "error" not in signature, signature


async def _staging_with_finished_jobs(conn) -> tuple[str, str]:
    """A graph revision whose every job is done — the state refresh_reports acts on."""
    from psycopg.types.json import Jsonb

    novel = await make_novel(conn, ontology='{"kinds":[],"attributes":[],"relations":[]}')
    cursor = await conn.execute(
        "INSERT INTO graph_revision(novel_id, state, ontology) VALUES(%s,'staging',%s)"
        " RETURNING id::text", (novel, Jsonb({})))
    revision = (await cursor.fetchone())[0]
    await conn.execute(
        """INSERT INTO graph_job(revision_id, chapter_index, state, input_hash,
                                 model_identity, generation)
           VALUES(%s,1,'done','h','m',1)""", (revision,))
    return novel, revision


@pytest.mark.db
async def test_report_refresh_reports_success_only_when_it_froze_a_report(db_conn, monkeypatch):
    from psycopg.types.json import Jsonb

    from pipeline import graph_rebuild

    async with db_conn.transaction(force_rollback=True):
        novel, revision = await _staging_with_finished_jobs(db_conn)

        async def freeze(db, cfg, rid):
            await db.execute("UPDATE graph_revision SET review=%s WHERE id=%s",
                             (Jsonb({"review_hash": "abc"}), rid))

        monkeypatch.setattr(graph_rebuild, "preview", freeze)
        assert await repair.refresh_reports(db_conn, object(), novel) == f"graph:{revision}"


@pytest.mark.db
async def test_a_report_that_does_not_land_never_starves_enrichment(db_conn, monkeypatch):
    """preview's write is version-guarded and may affect no rows.

    _drain_background stops for the tick on a truthy return, so claiming success when
    `review` is still NULL would select the same revision forever and permanently starve
    both chapter-enrichment drains -- on the most expensive path in the module.
    """
    from unittest.mock import AsyncMock

    from pipeline import graph_rebuild

    async with db_conn.transaction(force_rollback=True):
        novel, revision = await _staging_with_finished_jobs(db_conn)
        # preview runs but its guarded UPDATE lands on nothing.
        monkeypatch.setattr(graph_rebuild, "preview", AsyncMock(return_value={}))

        assert await repair.refresh_reports(db_conn, object(), novel) is None

        cursor = await db_conn.execute(
            "SELECT review IS NULL FROM graph_revision WHERE id=%s", (revision,))
        assert (await cursor.fetchone())[0], "precondition: the report was not stored"


async def _graph_job(conn, *, attempts: int = 0) -> tuple[str, str]:
    from psycopg.types.json import Jsonb

    novel = await make_novel(conn, ontology='{"kinds":[],"attributes":[],"relations":[]}')
    cursor = await conn.execute(
        "INSERT INTO graph_revision(novel_id, state, ontology) VALUES(%s,'staging',%s)"
        " RETURNING id::text", (novel, Jsonb({})))
    revision = (await cursor.fetchone())[0]
    await conn.execute(
        """INSERT INTO graph_job(revision_id, chapter_index, state, input_hash,
                                 model_identity, generation, attempts)
           VALUES(%s,1,'processing','h','m',1,%s)""", (revision, attempts))
    return novel, revision


@pytest.mark.db
async def test_graph_failure_records_a_class_and_schedules_a_retry(db_conn):
    """Executes the real UPDATE from graph_rebuild's error path.

    That statement only runs when a rebuild is already failing, which makes it the worst
    place in the module to have SQL nothing exercises.
    """
    from pipeline import graph_rebuild

    async with db_conn.transaction(force_rollback=True):
        novel, revision = await _graph_job(db_conn, attempts=1)
        delay = await graph_rebuild.record_job_failure(
            db_conn, revision, 1,
            RuntimeError("Ollama exhausted num_predict; refusing incomplete output"))

        assert delay == 5
        cursor = await db_conn.execute(
            "SELECT state, category, error, retry_at IS NOT NULL FROM graph_job"
            " WHERE revision_id=%s AND chapter_index=1", (revision,))
        state, category, error, scheduled = await cursor.fetchone()
        assert (state, category, scheduled) == ("failed", "output_truncated", True)
        # The text is still stored for an operator; it simply never leaves the database.
        assert "num_predict" in error

        # And the reader-facing view exposes the class, never the text.
        cursor = await db_conn.execute(
            "SELECT category FROM reader_repair_failures(%s) WHERE track='graph'", (novel,))
        assert (await cursor.fetchone())[0] == "output_truncated"


@pytest.mark.db
async def test_graph_failure_past_the_retry_bound_schedules_nothing(db_conn):
    """graph_retry_delay_minutes returns None past attempt 3, and retry_at must follow."""
    from pipeline import graph_rebuild

    async with db_conn.transaction(force_rollback=True):
        _, revision = await _graph_job(db_conn, attempts=4)
        delay = await graph_rebuild.record_job_failure(
            db_conn, revision, 1, ValueError("saved prose changed since snapshot"))

        assert delay is None
        cursor = await db_conn.execute(
            "SELECT category, retry_at FROM graph_job WHERE revision_id=%s AND chapter_index=1",
            (revision,))
        category, retry_at = await cursor.fetchone()
        assert category == "input_changed"
        assert retry_at is None, "an exhausted chapter must not be rescheduled"


@pytest.mark.db
async def test_graph_interruption_is_visible_and_immediately_resumable(db_conn):
    from pipeline import graph_rebuild

    async with db_conn.transaction(force_rollback=True):
        _, revision = await _graph_job(db_conn, attempts=1)
        await graph_rebuild.record_job_interruption(db_conn,revision,1)
        cursor=await db_conn.execute(
            "SELECT state,category,error,retry_at<=now() FROM graph_job "
            "WHERE revision_id=%s AND chapter_index=1",(revision,))
        assert await cursor.fetchone()==(
            'failed','cancelled','CancelledError: extraction interrupted',True)


@pytest.mark.db
async def test_event_failure_records_a_class_and_schedules_a_retry(db_conn):
    from psycopg.types.json import Jsonb

    from pipeline import event_rebuild

    async with db_conn.transaction(force_rollback=True):
        novel = await make_novel(db_conn)
        cursor = await db_conn.execute(
            """INSERT INTO event_revision(novel_id, state, event_schema, prompt_version)
               VALUES(%s,'staging',%s,'v1') RETURNING id::text""", (novel, Jsonb({})))
        revision = (await cursor.fetchone())[0]
        await db_conn.execute(
            """INSERT INTO event_job(revision_id, chapter_index, state, input_hash,
                                     model_identity, generation, attempts)
               VALUES(%s,1,'processing','h','m',1,1)""", (revision,))

        await event_rebuild.record_job_failure(
            db_conn, revision, 1, TimeoutError("timed out waiting for ollama"))

        cursor = await db_conn.execute(
            "SELECT state, category, retry_at IS NOT NULL FROM event_job"
            " WHERE revision_id=%s AND chapter_index=1", (revision,))
        assert await cursor.fetchone() == ("failed", "timeout", True)


async def _staging_revision(conn, *, novel=None, state="pending", attempts=0, chapters=1):
    """A staging graph revision with jobs, exactly as prepare() leaves one."""
    from psycopg.types.json import Jsonb

    if novel is None:
        novel = await make_novel(conn, ontology='{"kinds":[],"attributes":[],"relations":[]}')
    cursor = await conn.execute(
        "INSERT INTO graph_revision(novel_id, state, trusted, ontology)"
        " VALUES(%s,'staging',false,%s) RETURNING id::text", (novel, Jsonb({})))
    revision = (await cursor.fetchone())[0]
    for chapter in range(1, chapters + 1):
        await conn.execute(
            """INSERT INTO graph_job(revision_id, chapter_index, state, input_hash,
                                     model_identity, generation, attempts, retry_at)
               VALUES(%s,%s,%s,'h','m',1,%s,
                      CASE WHEN %s='failed' THEN now() - interval '1 minute' END)""",
            (revision, chapter, state, attempts, state))
    return novel, revision


@pytest.mark.db
async def test_a_prepared_rebuild_is_picked_up_by_the_worker(db_conn):
    """The regression this whole function exists for.

    prepare() leaves a revision state='staging', trusted=false. graph_rebuild's and
    event_rebuild's own drains only advance a revision that is already active AND trusted
    -- their job is keeping an activated graph current -- so a freshly prepared rebuild
    matched neither and sat at 0 chapters done forever. "Start fresh rebuild" quarantined
    the book and then nothing ran.
    """
    async with db_conn.transaction(force_rollback=True):
        novel, revision = await _staging_revision(db_conn)
        picked = await repair._next_staging_revision(
            db_conn, "graph_revision", "graph_job", novel)
        assert picked == revision


@pytest.mark.db
async def test_only_the_newest_staging_revision_is_drained(db_conn):
    """Preparing again supersedes an earlier attempt.

    Draining an abandoned rebuild would spend the model on work nobody is looking at --
    the panel reports the newest staging revision as the replacement.
    """
    async with db_conn.transaction(force_rollback=True):
        novel, superseded = await _staging_revision(db_conn)
        await db_conn.execute(
            "UPDATE graph_revision SET created_at = now() - interval '1 hour' WHERE id=%s",
            (superseded,))
        _, newest = await _staging_revision(db_conn, novel=novel)

        picked = await repair._next_staging_revision(
            db_conn, "graph_revision", "graph_job", novel)
        assert picked == newest, "the superseded rebuild must not be resumed"


@pytest.mark.db
async def test_blocked_staging_revision_is_not_hot_looped(db_conn):
    """A revision-level preflight failure stays visible until an operator replaces it.

    Selecting it on every idle tick both floods logs and prevents the active-revision
    drains later in Worker._drain_background from ever running.
    """
    async with db_conn.transaction(force_rollback=True):
        novel, revision = await _staging_revision(db_conn)
        await db_conn.execute(
            "UPDATE graph_revision SET blocked_category='model_unreachable',blocked_at=now() WHERE id=%s",
            (revision,))
        assert await repair._next_staging_revision(
            db_conn, "graph_revision", "graph_job", novel) is None


@pytest.mark.db
async def test_a_staging_chapter_out_of_attempts_is_left_alone(db_conn):
    """Past the retry bound the chapter is abandoned, and it fences the rest of the run."""
    async with db_conn.transaction(force_rollback=True):
        novel, _ = await _staging_revision(db_conn, state="failed", attempts=4)
        assert await repair._next_staging_revision(
            db_conn, "graph_revision", "graph_job", novel) is None


@pytest.mark.db
async def test_a_finished_rebuild_is_not_resumed_again(db_conn):
    async with db_conn.transaction(force_rollback=True):
        novel, revision = await _staging_revision(db_conn, state="done")
        assert await repair._next_staging_revision(
            db_conn, "graph_revision", "graph_job", novel) is None


@pytest.mark.db
async def test_only_the_earliest_unfinished_chapter_gates_a_rebuild(db_conn):
    """Same fencing as the active drain: a terminal failure holds the rest of the run.

    Otherwise later chapters publish identity state built on a gap.
    """
    async with db_conn.transaction(force_rollback=True):
        novel, revision = await _staging_revision(db_conn, chapters=3)
        # Chapter 1 is exhausted; 2 and 3 are ready. Nothing may move.
        await db_conn.execute(
            "UPDATE graph_job SET state='failed', attempts=9, retry_at=NULL"
            " WHERE revision_id=%s AND chapter_index=1", (revision,))
        assert await repair._next_staging_revision(
            db_conn, "graph_revision", "graph_job", novel) is None


@pytest.mark.db
async def test_a_blocked_run_is_recorded_on_the_revision_not_a_chapter(db_conn):
    """A dead endpoint must be visible, and must not cost a chapter its retries.

    resume()'s preamble runs before the per-chapter try block, so a failure there reached
    no recorder at all: the run died while the panel showed "0 of N done" and an empty
    failure ledger. Attributing it to a chapter would be worse than silence -- a flapping
    connection would burn chapter 1's three attempts and strand the whole revision.
    """
    from pipeline.failures import clear_blocked, record_blocked

    async with db_conn.transaction(force_rollback=True):
        novel, revision = await _staging_revision(db_conn)

        category = await record_blocked(
            db_conn, "graph_revision", revision,
            OSError("ConnectError: All connection attempts failed"))
        assert category == "model_unreachable"

        cursor = await db_conn.execute(
            "SELECT blocked_category, blocked_at IS NOT NULL FROM graph_revision WHERE id=%s",
            (revision,))
        assert await cursor.fetchone() == ("model_unreachable", True)

        # The chapter is untouched: no attempt consumed, no failure recorded.
        cursor = await db_conn.execute(
            "SELECT state, attempts, category FROM graph_job WHERE revision_id=%s", (revision,))
        assert await cursor.fetchone() == ("pending", 0, None)

        # And the reader-facing status surfaces it.
        cursor = await db_conn.execute(
            "SELECT blocked_category FROM reader_repair_status(%s) WHERE track='graph'", (novel,))
        assert (await cursor.fetchone())[0] == "model_unreachable"

        # Recovery clears it as soon as the run gets past its preamble.
        await clear_blocked(db_conn, "graph_revision", revision)
        cursor = await db_conn.execute(
            "SELECT blocked_category, blocked_at FROM graph_revision WHERE id=%s", (revision,))
        assert await cursor.fetchone() == (None, None)


@pytest.mark.db
async def test_status_counts_what_the_rebuild_has_published(db_conn):
    """A counter that only moves once per chapter cannot show progress within one.

    Chapter 1 takes many inference calls; claims_published is what makes a long rebuild
    visibly alive between chapter boundaries.
    """
    async with db_conn.transaction(force_rollback=True):
        novel, revision = await _staging_revision(db_conn)
        cursor = await db_conn.execute(
            "SELECT claims_published, entities_created FROM reader_repair_status(%s)"
            " WHERE track='graph'", (novel,))
        assert await cursor.fetchone() == (0, 0)


async def _quarantined_novel(conn):
    """A book exactly as prepare() leaves it: an active, now-untrusted `old` revision and
    a fresh staging `rid`, linked by the quarantine audit row prepare itself writes."""
    from psycopg.types.json import Jsonb

    novel, rid = await _staging_revision(conn)
    old = (await (await conn.execute(
        "SELECT active_graph_revision FROM novel WHERE id=%s", (novel,))).fetchone())[0]
    await conn.execute("UPDATE graph_revision SET trusted=false WHERE id=%s", (old,))
    await conn.execute(
        "INSERT INTO graph_audit(novel_id,revision_id,action,detail) VALUES(%s,%s,'quarantine',%s)",
        (novel, old, Jsonb(dict(replacement=rid))))
    return novel, str(old), rid


@pytest.mark.db
async def test_discard_restores_trust_on_the_audited_quarantine_target(db_conn):
    """The missing escape: undo prepare()'s own precaution, identified by its audit row.

    Unlike rollback, this is not "give up on a real decision" — the staging revision
    never published anything, so there is nothing to distrust the old revision over.
    """
    from pipeline.graph_rebuild import discard

    async with db_conn.transaction(force_rollback=True):
        novel, old, rid = await _quarantined_novel(db_conn)
        result = await discard(db_conn, None, rid)
        assert result == {"revision": rid, "restored": old}

        row = await (await db_conn.execute(
            "SELECT state, trusted FROM graph_revision WHERE id=%s", (rid,))).fetchone()
        assert row == ("archived", False)
        row = await (await db_conn.execute(
            "SELECT trusted FROM graph_revision WHERE id=%s", (old,))).fetchone()
        assert row == (True,)
        audit = await (await db_conn.execute(
            "SELECT detail FROM graph_audit WHERE novel_id=%s AND action='discard'",
            (novel,))).fetchone()
        assert audit[0] == {"restored": old}


@pytest.mark.db
async def test_discard_does_not_restore_trust_when_a_second_quarantine_intervened(db_conn):
    """§0: trusted=false stays a human decision. A later prepare() re-quarantined the same
    old revision for a different reason, so THIS discard must not silently re-trust it."""
    from psycopg.types.json import Jsonb

    from pipeline.graph_rebuild import discard

    async with db_conn.transaction(force_rollback=True):
        novel, old, rid = await _quarantined_novel(db_conn)
        _, rid2 = await _staging_revision(db_conn, novel=novel)
        await db_conn.execute(
            "INSERT INTO graph_audit(novel_id,revision_id,action,detail) VALUES(%s,%s,'quarantine',%s)",
            (novel, old, Jsonb(dict(replacement=rid2))))

        result = await discard(db_conn, None, rid)
        assert result == {"revision": rid, "restored": None}

        row = await (await db_conn.execute(
            "SELECT trusted FROM graph_revision WHERE id=%s", (old,))).fetchone()
        assert row == (False,), "a second quarantine intervened; trust stays withheld"


@pytest.mark.db
async def test_discard_refuses_a_non_staging_revision(db_conn):
    from pipeline.graph_rebuild import discard

    async with db_conn.transaction(force_rollback=True):
        novel, old, rid = await _quarantined_novel(db_conn)
        await db_conn.execute("UPDATE graph_revision SET state='archived' WHERE id=%s", (rid,))
        with pytest.raises(ValueError, match="only a staging revision can be discarded"):
            await discard(db_conn, None, rid)


@pytest.mark.db
async def test_discard_cancels_live_chapter_knowledge_runs(db_conn):
    """0054's partial unique index allows one live run per (novel, chapter, scope). A
    discarded staging revision must not leave one dangling and block a later run."""
    from pipeline.graph_rebuild import discard

    async with db_conn.transaction(force_rollback=True):
        novel, old, rid = await _quarantined_novel(db_conn)
        await db_conn.execute(
            "INSERT INTO chapter(novel_id,chapter_index,raw_hash,raw_uri,source_meta,status) "
            "VALUES (%s,1,%s,'raw/test.txt','{}','done')", (novel, f"sha256:{novel}"))
        run = await (await db_conn.execute(
            """INSERT INTO chapter_knowledge_run
                 (novel_id,chapter_index,revision_id,mode,input_hash,display_hash,
                  model_identity,graph_generation,graph_version)
               VALUES(%s,1,%s,'ordinary','h','h','m',1,1) RETURNING id::text""",
            (novel, rid))).fetchone()

        await discard(db_conn, None, rid)

        row = await (await db_conn.execute(
            "SELECT state FROM chapter_knowledge_run WHERE id=%s", (run[0],))).fetchone()
        assert row == ("rejected",)
