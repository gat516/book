"""Queue scheduling/recovery regressions against isolated Redis keys, no LLM calls."""

import asyncio
import json
import os
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
import redis.asyncio as redis

from fixtures import make_novel, delete_novel, make_config
from pipeline import queue
from pipeline.failures import record_failure, error_code
from pipeline.worker import Worker, ChapterFailed, NovelDeleted


async def keep_novel_alive(novel_id):
    await asyncio.Event().wait()


@pytest_asyncio.fixture
async def scheduled(monkeypatch):
    url = os.getenv("PIPELINE_TEST_REDIS_URL")
    if not url:
        pytest.skip("PIPELINE_TEST_REDIS_URL is not set")
    client = redis.from_url(url, decode_responses=True)
    prefix = f"test:worker:{uuid.uuid4()}:"
    keys = [prefix + str(i) for i in range(len(queue.KEYS))]
    monkeypatch.setattr(queue, "KEYS", keys)
    try:
        yield client, keys
    finally:
        await client.delete(*keys)
        await client.aclose()


def message(n, novel="a", priority=False):
    value = {"novel_id": novel, "chapter_index": n}
    if priority:
        value["priority"] = True
    return json.dumps(value)


async def call(client, script, *args):
    return await client.eval(script, len(queue.KEYS), *queue.KEYS, *args)


async def test_order_retries_before_later_chapters_and_keep_novel_fifo(scheduled):
    client, keys = scheduled
    # Oldest queued novel A has shuffled chapters; B must not jump ahead just
    # because its chapter number is smaller. Simulate completion after each claim.
    await client.lpush(keys[0], message(10), message(12), message(7), message(1, "b"))
    for raw in (message(7), message(10), message(12), message(1, "b")):
        assert await call(client, queue.CLAIM, "100") == raw
        await call(client, queue.RELEASE, raw, "100", "done")
    assert await call(client, queue.CLAIM, "101") is None


async def test_explicit_priority_wins_once_and_deduplicates(scheduled):
    client, keys = scheduled
    priority = message(12, priority=True)
    await client.lpush(keys[0], message(7), message(12), priority)
    assert await call(client, queue.CLAIM, "100") == priority
    assert await client.lrange(keys[0], 0, -1) == [message(7)]
    await call(client, queue.RELEASE, priority, "100", "done")
    assert await call(client, queue.CLAIM, "101") == message(7)


async def test_same_novel_cannot_run_twice_but_other_novel_can(scheduled):
    client, keys = scheduled
    await client.lpush(keys[1], message(2))
    await client.lpush(keys[0], message(3), message(4, priority=True), message(1, "b"))
    assert await call(client, queue.CLAIM, "100") == message(1, "b")
    assert await call(client, queue.CLAIM, "101") is None


async def test_heartbeat_protects_slow_job_then_crash_recovers_once(scheduled):
    client, keys = scheduled
    raw = message(7)
    await client.lpush(keys[0], raw)
    assert await call(client, queue.CLAIM, "100") == raw
    assert await call(client, queue.RENEW, raw, "100", "500") == 1
    assert await call(client, queue.REAP, raw, "501", "300") == 0
    assert await client.hget(keys[2], raw) == "100"  # UI timer stays chapter-wide
    assert await call(client, queue.REAP, raw, "801", "300") == 1
    assert await call(client, queue.REAP, raw, "802", "300") == 0
    assert await call(client, queue.CLAIM, "900") == raw
    # An old worker cannot renew or acknowledge a newly acquired claim.
    assert await call(client, queue.RENEW, raw, "100", "901") == 0
    assert await call(client, queue.RELEASE, raw, "100", "done") == 0
    assert await client.lrange(keys[1], 0, -1) == [raw]


async def test_legacy_claim_without_heartbeat_is_recoverable(scheduled):
    client, keys = scheduled
    raw = message(10)
    await client.lpush(keys[1], raw)
    await client.hset(keys[2], raw, "100")
    assert await call(client, queue.REAP, raw, "401", "300") == 1
    assert await client.lrange(keys[0], 0, -1) == [raw]


async def test_worker_reaper_does_not_enqueue_twice(scheduled, monkeypatch):
    import pipeline.worker as module
    client, keys = scheduled
    monkeypatch.setattr(module, "PROCESSING_STARTED", keys[2])
    worker = Worker.__new__(Worker)
    worker.redis = client
    worker.cfg = SimpleNamespace(visibility_timeout=1)
    raw = message(7)
    await client.lpush(keys[1], raw)
    await client.hset(keys[2], raw, "1")
    await worker._reap_once()
    assert await client.lrange(keys[0], 0, -1) == [raw]


@pytest.mark.parametrize("fail", [False, True])
async def test_shutdown_finishes_current_claim_and_preserves_pending(scheduled, fail):
    client, keys = scheduled
    worker = Worker.__new__(Worker)
    worker.redis = client
    worker.stopping = asyncio.Event()
    worker.cfg = SimpleNamespace(queue_timeout=1, visibility_timeout=300)
    worker._watch_novel = keep_novel_alive
    seen = []
    async def handle(raw):
        seen.append(raw)
        worker.request_stop()
        if fail:
            raise ChapterFailed("recorded failure")
    worker._handle = handle
    await client.lpush(keys[0], message(2), message(3))
    await worker._loop()
    assert seen == [message(2)]
    assert await client.lrange(keys[0], 0, -1) == [message(3)]
    assert await client.llen(keys[1]) == 0
    assert await client.hlen(keys[2]) == 0
    assert await client.hlen(keys[4]) == 0


async def test_completed_pointer_does_not_repeat_any_model_work():
    worker = Worker.__new__(Worker)
    worker.db = object()
    worker._fetch_one = AsyncMock(return_value=("hash", "uri", {}, "done", True, "saved"))
    await worker._handle(message(4))
    worker._fetch_one.assert_awaited_once()


async def test_runtime_reservation_requeues_without_losing_chapter(scheduled):
    from novel_llm import AdmissionRejected
    client,keys=scheduled
    worker=Worker.__new__(Worker)
    worker.redis=client
    worker.stopping=asyncio.Event()
    worker.cfg=SimpleNamespace(queue_timeout=1,visibility_timeout=300)
    worker._watch_novel=keep_novel_alive
    async def handle(raw):
        worker.request_stop()
        raise AdmissionRejected(retry_after_s=0)
    worker._handle=handle
    await client.lpush(keys[0],message(2),message(3))
    await worker._loop()
    assert set(await client.lrange(keys[0],0,-1))=={message(2),message(3)}
    assert await client.llen(keys[1])==0
    assert await client.hlen(keys[4])==0


@pytest.mark.db
@pytest.mark.parametrize("transient_outage", [False, True])
async def test_deletion_cancels_inference_releases_claim_and_runs_next_novel(
    scheduled, db_conn, monkeypatch, tmp_path, transient_outage
):
    from novel_llm.admission import ollama_session
    import pipeline.worker as module

    client, keys = scheduled
    monkeypatch.setattr(module, "NOVEL_CHECK_SECONDS", .02)
    monkeypatch.setattr(module, "PROCESSING_STAGE", keys[3])
    monkeypatch.setenv("BOOK_OLLAMA_LOCK_DIR", str(tmp_path))
    outage_seen = asyncio.Event()
    if transient_outage:
        connect = module.psycopg.AsyncConnection.connect
        attempts = 0
        async def unavailable_once(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                outage_seen.set()
                raise module.psycopg.OperationalError("temporary connection outage")
            return await connect(*args, **kwargs)
        monkeypatch.setattr(module.psycopg.AsyncConnection, "connect", unavailable_once)
    deleted = await make_novel(db_conn)
    next_novel = await make_novel(db_conn)
    worker = Worker.__new__(Worker)
    worker.redis = client
    worker.stopping = asyncio.Event()
    worker.cfg = make_config(database_url=os.getenv(
        "DATABASE_URL", "postgres://engine:engine@localhost:5432/novel_engine"
    ))
    entered, cancelled = asyncio.Event(), asyncio.Event()
    preview = module.PREVIEW_KEY.format(novel_id=deleted, chapter_index=1)
    served = []

    async def handle(raw):
        msg = json.loads(raw)
        async with ollama_session("http://deletion-test", timeout=0):
            if msg["novel_id"] == deleted:
                await client.hset(keys[3], raw, "translate")
                await client.set(preview, "draft from the deleted novel")
                entered.set()
                try:
                    # Represents an in-flight provider call, not a stage boundary.
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            else:
                served.append(msg["novel_id"])
                worker.request_stop()

    worker._handle = handle
    await client.lpush(keys[0], message(1, deleted), message(1, next_novel))
    run = asyncio.create_task(worker._loop())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if transient_outage:
            await asyncio.wait_for(outage_seen.wait(), 5)
            await asyncio.sleep(.01)
            assert not cancelled.is_set()
        # Real committed cascade, with no Redis deletion signal: DB is authoritative.
        await db_conn.execute("DELETE FROM novel WHERE id=%s", (deleted,))
        await asyncio.wait_for(run, 5)
        assert cancelled.is_set()
        assert served == [next_novel]  # also proves the model admission lock was released
        assert not await client.exists(preview)
        assert await client.lrange(keys[0], 0, -1) == []
        assert await client.lrange(keys[1], 0, -1) == []
        for key in keys[2:]:
            assert await client.hgetall(key) == {}
        assert await (await db_conn.execute(
            "SELECT 1 FROM chapter_failure WHERE novel_id=%s", (deleted,)
        )).fetchone() is None
    finally:
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)
        await client.delete(preview)
        await delete_novel(db_conn, deleted)
        await delete_novel(db_conn, next_novel)


async def test_claim_cancellation_waits_for_work_and_watcher_cleanup():
    worker = Worker.__new__(Worker)
    started, cleaned = asyncio.Event(), asyncio.Event()
    worker._watch_novel = keep_novel_alive
    async def handle(raw):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.set()
    worker._handle = handle
    task = asyncio.create_task(worker._handle_claim(message(1)))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set()


@pytest.mark.db
async def test_deleted_novel_stage_error_does_not_write_failure_history(db_conn, monkeypatch):
    import pipeline.worker as module
    novel = await make_novel(db_conn)
    worker = Worker.__new__(Worker)
    worker.db, worker.cfg = db_conn, make_config()
    worker.redis = AsyncMock()
    worker.minio = worker.cache = worker.textproc = worker.embed_provider = None
    worker._provider_for_novel = AsyncMock(return_value=(object(), object(), "ollama"))
    worker._get_object = lambda uri: "source"
    class DeletedDuringStage:
        name = "translate"
        async def run(self, ctx, state):
            await db_conn.execute("DELETE FROM novel WHERE id=%s", (novel,))
            raise ValueError("stage failed after its owner was deleted")
    monkeypatch.setattr(module, "DEFAULT_STAGES", [DeletedDuringStage()])
    try:
        await db_conn.execute(
            "INSERT INTO chapter(novel_id,chapter_index,raw_hash,raw_uri,source_meta,status) "
            "VALUES (%s,1,'test','raw','{}','queued')", (novel,)
        )
        # Exercise the race where a write fails before the watcher's next check.
        with pytest.raises(NovelDeleted):
            await worker._handle(message(1, novel))
        assert await (await db_conn.execute(
            "SELECT 1 FROM chapter_failure WHERE novel_id=%s", (novel,)
        )).fetchone() is None
    finally:
        await delete_novel(db_conn, novel)


def test_failure_categories_do_not_include_exception_payload():
    assert error_code(httpx.ReadTimeout("private URL")) == "provider_timeout"
    assert error_code(httpx.ConnectError("credentials")) == "provider_connection"
    assert error_code(ValueError("source text")) == "invalid_stage_output"


@pytest.mark.db
async def test_failure_history_survives_retry_without_recording_private_payload(db_conn):
    novel = await make_novel(db_conn)
    try:
        await record_failure(db_conn, novel, 7, "state", ValueError("private story text"))
        await record_failure(db_conn, novel, 7, "state", httpx.ReadTimeout("private key"))
        rows = await (await db_conn.execute(
            "SELECT stage, error_type, error_code FROM chapter_failure WHERE novel_id=%s ORDER BY id",
            (novel,),
        )).fetchall()
        assert rows == [("state", "ValueError", "invalid_stage_output"),
                        ("state", "ReadTimeout", "provider_timeout")]
    finally:
        await delete_novel(db_conn, novel)


@pytest.mark.db
@pytest.mark.parametrize("failed_stage", ["resolve", "translate", "state", "graph_write"])
async def test_enrichment_failure_cannot_hide_valid_translation(db_conn, monkeypatch, failed_stage):
    import pipeline.worker as module
    novel = await make_novel(db_conn)
    worker = Worker.__new__(Worker)
    worker.db, worker.cfg = db_conn, make_config()
    worker.redis = AsyncMock()
    worker.minio = worker.cache = worker.textproc = worker.embed_provider = None
    worker._provider_for_novel = AsyncMock(return_value=(object(), object(), "ollama"))
    worker._get_object = lambda uri: "saved prose" if uri == "saved" else "original text"
    calls = []
    class Stage:
        def __init__(self, name): self.name = name
        async def run(self, ctx, state):
            calls.append(self.name)
            if self.name == failed_stage:
                raise ValueError("invalid model output")
            if self.name == "translate":
                state.translation = "saved prose"
                await db_conn.execute("UPDATE chapter SET translated_uri='saved' WHERE novel_id=%s", (novel,))
            if self.name in {"state", "graph_write"}:
                row = await (await db_conn.execute("SELECT translation_ready FROM chapter WHERE novel_id=%s", (novel,))).fetchone()
                assert row[0] is True  # readable BEFORE optional work completes
    monkeypatch.setattr(module, "DEFAULT_STAGES", [Stage(n) for n in ("resolve", "translate", "display_scan", "state", "graph_write")])
    try:
        await db_conn.execute("INSERT INTO chapter(novel_id,chapter_index,raw_hash,raw_uri,source_meta,status) VALUES (%s,1,'test','raw','{}','queued')", (novel,))
        with pytest.raises(ChapterFailed):
            await worker._handle(message(1, novel))
        row = await (await db_conn.execute("SELECT status,translation_ready,enrichment_retry_at IS NOT NULL,translated_uri FROM chapter WHERE novel_id=%s", (novel,))).fetchone()
        readable = failed_stage != "translate"
        assert row == ("error", readable, readable, "saved" if readable else None)
        failure = await (await db_conn.execute("SELECT stage FROM chapter_failure WHERE novel_id=%s", (novel,))).fetchone()
        assert failure[0] == failed_stage
        if failed_stage == "resolve":
            assert calls == ["resolve", "translate", "display_scan"]  # names need no identity; no partially bound graph writes
        else:
            # Graph retries retain the saved text, even if terminology has since changed.
            class SuccessfulStage(Stage):
                async def run(self, ctx, state):
                    if self.name == "translate" and readable:
                        pytest.fail("enrichment retry must not retranslate")
                    if self.name == "graph_write" and readable:
                        assert state.translation == "saved prose"
            if readable:
                monkeypatch.setattr(module, "DEFAULT_STAGES", [SuccessfulStage(n) for n in ("resolve", "translate", "state", "graph_write")])
                await worker._handle(message(1, novel))
                row = await (await db_conn.execute("SELECT status,translation_ready,enrichment_retry_at FROM chapter WHERE novel_id=%s", (novel,))).fetchone()
                assert row == ("done", True, None)
    finally:
        await delete_novel(db_conn, novel)


async def test_enrichment_retries_are_deduplicated_and_yield_to_reading(scheduled):
    client, keys = scheduled
    retry = json.dumps({"novel_id": "a", "chapter_index": 2, "enrichment": True})
    assert await call(client, queue.ENQUEUE_ENRICHMENT, "a", 2, retry) == 1
    assert await call(client, queue.ENQUEUE_ENRICHMENT, "a", 2, retry) == 0
    await client.lpush(keys[0], message(4))
    assert await call(client, queue.CLAIM, "100") == message(4)
    await call(client, queue.RELEASE, message(4), "100", "done")
    assert await call(client, queue.CLAIM, "101") == retry
    assert await call(client, queue.ENQUEUE_ENRICHMENT, "a", 2, retry) == 0
