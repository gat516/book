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
from pipeline.worker import Worker, ChapterFailed, NovelDeleted, TranslationPublished


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


async def test_release_clears_current_stage_timer(scheduled):
    client, keys = scheduled
    raw = message(1)
    await client.lpush(keys[0], raw)
    assert await call(client, queue.CLAIM, "100") == raw
    await client.hset(keys[6], raw, "101")
    await call(client, queue.RELEASE, raw, "100", "done")
    assert await client.hexists(keys[6], raw) == 0


async def test_translation_release_atomically_requeues_low_priority_enrichment(scheduled):
    client, keys = scheduled
    raw = message(1)
    enrichment = json.dumps({"novel_id": "a", "chapter_index": 1, "enrichment": True})
    await client.lpush(keys[0], raw)
    assert await call(client, queue.CLAIM, "100") == raw
    assert await call(client, queue.RELEASE, raw, "100", "enrich", enrichment) == 1
    assert await client.lrange(keys[1], 0, -1) == []
    assert await client.lrange(keys[0], 0, -1) == [enrichment]


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


async def test_book_focus_beats_old_priority_and_switch_keeps_pending_work(scheduled):
    client, keys = scheduled
    old_priority = message(7, "a", priority=True)
    await client.lpush(keys[0], old_priority, message(8, "a"), message(3, "b"), message(1, "b"))
    await client.hset(keys[5], mapping={"focus_novel_id": "b", "mode": "all"})
    assert await call(client, queue.CLAIM, "100") == message(1, "b")
    # A switch changes the next selection, never the claim that is already running.
    await client.hset(keys[5], "focus_novel_id", "a")
    assert await client.lrange(keys[1], 0, -1) == [message(1, "b")]
    await call(client, queue.RELEASE, message(1, "b"), "100", "done")
    assert await call(client, queue.CLAIM, "101") == old_priority
    await call(client, queue.RELEASE, old_priority, "101", "done")
    assert await call(client, queue.CLAIM, "102") == message(8, "a")
    await call(client, queue.RELEASE, message(8, "a"), "102", "done")
    assert await call(client, queue.CLAIM, "103") == message(3, "b")


async def test_focused_mode_and_pause_preserve_queue_and_claims(scheduled):
    client, keys = scheduled
    await client.lpush(keys[0], message(1, "a", priority=True), message(1, "b"), message(2, "b"))
    await client.hset(keys[5], mapping={"mode": "focused", "focus_novel_id": "b"})
    current = await call(client, queue.CLAIM, "100")
    assert current == message(1, "b")
    assert await call(client, queue.CLAIM, "101") is None  # no background fallback
    await client.hset(keys[5], "mode", "paused")
    pending = await client.lrange(keys[0], 0, -1)
    assert await call(client, queue.CLAIM, "102") is None
    assert await client.lrange(keys[0], 0, -1) == pending
    assert await call(client, queue.RENEW, current, "100", "103") == 1
    await call(client, queue.RELEASE, current, "100", "done")
    assert await call(client, queue.CLAIM, "104") is None
    await client.hset(keys[5], "mode", "focused")
    assert await call(client, queue.CLAIM, "105") == message(2, "b")
    await call(client, queue.RELEASE, message(2, "b"), "105", "done")
    assert await call(client, queue.CLAIM, "106") is None
    await client.hdel(keys[5], "focus_novel_id")
    assert await call(client, queue.CLAIM, "107") is None
    await client.hset(keys[5], "mode", "all")
    assert await call(client, queue.CLAIM, "108") == message(1, "a", priority=True)


async def test_queue_controls_also_gate_idle_graph_work(scheduled, monkeypatch):
    from pipeline import event_rebuild, graph_rebuild, repair
    client, keys = scheduled
    worker = Worker.__new__(Worker)
    worker.redis, worker.cfg = client, make_config()
    drain = AsyncMock()
    drain_events = AsyncMock()
    drain_requests = AsyncMock(return_value=None)
    monkeypatch.setattr(graph_rebuild, "drain_active", drain)
    monkeypatch.setattr(event_rebuild, "drain_active", drain_events)
    monkeypatch.setattr(repair, "drain_requests", drain_requests)
    for mode in ("paused", "focused"):
        await client.hset(keys[5], "mode", mode)
        await worker._drain_background()
        drain.assert_not_awaited()
        drain_events.assert_not_awaited()
    await client.hset(keys[5], "focus_novel_id", "b")
    await worker._drain_background()
    drain_events.assert_awaited_once_with(worker.cfg, novel_id="b")
    drain.assert_awaited_once_with(worker.cfg, novel_id="b", preferred_novel="b")
    drain.reset_mock()
    drain_events.reset_mock()
    await client.hset(keys[5], "mode", "all")
    await worker._drain_background()
    drain_events.assert_awaited_once_with(worker.cfg, novel_id=None)
    drain.assert_awaited_once_with(worker.cfg, novel_id=None, preferred_novel="b")
    assert drain_requests.await_count == 3


async def test_embedding_probe_is_memoized_after_a_successful_chapter_check():
    worker = Worker.__new__(Worker)
    worker.cfg = SimpleNamespace(embed_dim=3, embed_model="test-embed")
    worker._embeddings_ready = False
    worker.embed_provider = SimpleNamespace(embed=AsyncMock(return_value=[[0.0, 0.0, 0.0]]))

    await worker._ensure_embeddings()
    await worker._ensure_embeddings()

    worker.embed_provider.embed.assert_awaited_once_with(["dimension probe"])
    assert worker._embeddings_ready is True


async def test_embedding_transport_is_retryable_and_has_stable_category():
    from novel_llm import AdmissionRejected

    worker = Worker.__new__(Worker)
    worker.cfg = SimpleNamespace(embed_dim=3, embed_model="test-embed")
    worker._embeddings_ready = False
    worker.embed_provider = SimpleNamespace(
        embed=AsyncMock(side_effect=AdmissionRejected("provider unreachable: ConnectError",
                                                      retry_after_s=7.5))
    )

    with pytest.raises(AdmissionRejected, match="^embed_unavailable$") as caught:
        await worker._ensure_embeddings()

    assert caught.value.category == "embed_unavailable"
    assert caught.value.retry_after_s == 7.5
    assert worker._embeddings_ready is False


async def test_embedding_dimension_drift_remains_fatal():
    worker = Worker.__new__(Worker)
    worker.cfg = SimpleNamespace(embed_dim=3, embed_model="test-embed")
    worker._embeddings_ready = False
    worker.embed_provider = SimpleNamespace(embed=AsyncMock(return_value=[[0.0, 0.0]]))

    with pytest.raises(RuntimeError, match="returned 2 dims"):
        await worker._ensure_embeddings()

    assert worker._embeddings_ready is False


async def test_embedding_dimension_drift_escapes_the_worker_loop(scheduled, monkeypatch):
    import pipeline.worker as module

    client, keys = scheduled
    heartbeat = keys[-1] + ":heartbeat"
    monkeypatch.setattr(module, "WORKER_HEARTBEAT", heartbeat)
    worker = Worker.__new__(Worker)
    worker.redis = client
    worker.stopping = asyncio.Event()
    worker.cfg = SimpleNamespace(queue_timeout=1, visibility_timeout=300)
    worker._deferrals = 0
    worker._ensure_embeddings = AsyncMock(
        side_effect=module.EmbeddingDimensionMismatch("embedding dimension drift")
    )
    worker._handle = AsyncMock()
    raw = message(2)
    await client.lpush(keys[0], raw)

    with pytest.raises(module.EmbeddingDimensionMismatch, match="dimension drift"):
        await worker._loop()

    worker._handle.assert_not_awaited()
    await client.delete(heartbeat)


async def test_worker_start_does_not_require_embeddings(monkeypatch):
    import pipeline.worker as module

    worker = Worker.__new__(Worker)
    worker.cfg = SimpleNamespace(database_url="postgres://test")
    worker.stopping = asyncio.Event()
    worker.textproc = SimpleNamespace(aclose=AsyncMock())
    worker._ensure_embeddings = AsyncMock(
        side_effect=module.AdmissionRejected("embed_unavailable")
    )
    connection = SimpleNamespace(close=AsyncMock())

    async def run_loop():
        worker.request_stop()

    worker._loop = run_loop
    worker._reap_forever = AsyncMock()
    connect = AsyncMock(return_value=connection)
    monkeypatch.setattr(module.psycopg.AsyncConnection, "connect", connect)

    await worker.start()

    connect.assert_awaited_once_with("postgres://test", autocommit=True)
    assert connection.close.await_count == 1
    worker._ensure_embeddings.assert_not_awaited()


async def test_worker_loop_heartbeats_and_drains_background_when_embeddings_are_down(
    scheduled, monkeypatch
):
    import pipeline.worker as module

    client, keys = scheduled
    heartbeat = keys[-1] + ":heartbeat"
    monkeypatch.setattr(module, "WORKER_HEARTBEAT", heartbeat)
    worker = Worker.__new__(Worker)
    worker.redis = client
    worker.stopping = asyncio.Event()
    worker.cfg = SimpleNamespace(queue_timeout=1, visibility_timeout=300)
    worker._ensure_embeddings = AsyncMock(side_effect=module.AdmissionRejected("embed_unavailable"))

    async def drain_background():
        worker.request_stop()

    worker._drain_background = AsyncMock(side_effect=drain_background)
    await worker._loop()

    assert await client.get(heartbeat) is not None
    worker._drain_background.assert_awaited_once_with()
    worker._ensure_embeddings.assert_not_awaited()
    await client.delete(heartbeat)


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
async def test_shutdown_preserves_work_that_completes_with_the_signal(scheduled, fail):
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


async def test_shutdown_cancels_current_claim_and_requeues_after_cleanup(scheduled):
    client, keys = scheduled
    worker = Worker.__new__(Worker)
    worker.redis = client
    worker.stopping = asyncio.Event()
    worker.cfg = SimpleNamespace(queue_timeout=1, visibility_timeout=300)
    worker._watch_novel = keep_novel_alive
    entered, cleaned = asyncio.Event(), asyncio.Event()

    async def handle(raw):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            # Represents provider/transaction cleanup that must finish before RELEASE.
            await asyncio.sleep(0)
            cleaned.set()

    worker._handle = handle
    raw = message(2)
    await client.lpush(keys[0], raw)
    run = asyncio.create_task(worker._loop())
    await asyncio.wait_for(entered.wait(), 1)
    worker.request_stop()
    await asyncio.wait_for(run, 1)

    assert cleaned.is_set()
    assert await client.lrange(keys[0], 0, -1) == [raw]
    assert await client.llen(keys[1]) == 0
    assert await client.hlen(keys[2]) == 0
    assert await client.hlen(keys[4]) == 0


async def test_shutdown_cancels_background_rebuild_promptly(scheduled):
    client, _ = scheduled
    worker = Worker.__new__(Worker)
    worker.redis = client
    worker.stopping = asyncio.Event()
    worker.cfg = SimpleNamespace(queue_timeout=1)
    entered, cleaned = asyncio.Event(), asyncio.Event()

    async def drain_background():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            # graph_rebuild.resume records the interrupted job in this cleanup path.
            await asyncio.sleep(0)
            cleaned.set()

    worker._drain_background = drain_background
    run = asyncio.create_task(worker._loop())
    await asyncio.wait_for(entered.wait(), 1)
    worker.request_stop()
    await asyncio.wait_for(run, 1)

    assert cleaned.is_set()


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


async def test_embed_unavailable_requeues_claim_without_running_chapter(scheduled, monkeypatch):
    import pipeline.worker as module
    from novel_llm import AdmissionRejected

    client, keys = scheduled
    heartbeat = keys[-1] + ":heartbeat"
    monkeypatch.setattr(module, "WORKER_HEARTBEAT", heartbeat)
    worker = Worker.__new__(Worker)
    worker.redis = client
    worker.stopping = asyncio.Event()
    worker.cfg = SimpleNamespace(queue_timeout=1, visibility_timeout=300)
    worker._deferrals = 0
    raw = message(2)
    await client.lpush(keys[0], raw)

    async def reject_probe():
        worker.request_stop()
        raise AdmissionRejected("embed_unavailable", retry_after_s=0)

    worker._ensure_embeddings = reject_probe
    worker._handle = AsyncMock()
    await worker._loop()

    assert await client.get(heartbeat) is not None
    assert await client.lrange(keys[0], 0, -1) == [raw]
    assert await client.llen(keys[1]) == 0
    worker._handle.assert_not_awaited()
    await client.delete(heartbeat)


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
    worker._ensure_embeddings = AsyncMock()
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
    worker._ensure_embeddings = AsyncMock()
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
    worker._provider_for_novel = AsyncMock(
        return_value=(object(), object(), "ollama", None, None, None)
    )
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
@pytest.mark.parametrize(
    "failed_stage", ["translate", "character_names", "resolve", "state", "graph_write"]
)
async def test_enrichment_failure_cannot_hide_valid_translation(db_conn, monkeypatch, failed_stage):
    import pipeline.worker as module
    novel = await make_novel(db_conn)
    worker = Worker.__new__(Worker)
    worker.db, worker.cfg = db_conn, make_config()
    worker.redis = AsyncMock()
    worker.minio = worker.cache = worker.textproc = worker.embed_provider = None
    worker._provider_for_novel = AsyncMock(
        return_value=(object(), object(), "ollama", None, None, None)
    )
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
    monkeypatch.setattr(module, "DEFAULT_STAGES", [Stage(n) for n in (
        "translate", "character_names", "resolve", "display_scan", "state", "graph_write")])
    try:
        readable = failed_stage != "translate"
        await db_conn.execute(
            "INSERT INTO chapter(novel_id,chapter_index,raw_hash,raw_uri,source_meta,status,"
            "translation_ready,translated_uri) VALUES (%s,1,'test','raw','{}','queued',%s,%s)",
            (novel, readable, "saved" if readable else None),
        )
        raw = json.dumps({"novel_id": novel, "chapter_index": 1, "enrichment": readable})
        with pytest.raises(ChapterFailed):
            await worker._handle(raw)
        row = await (await db_conn.execute("SELECT status,translation_ready,enrichment_retry_at IS NOT NULL,translated_uri FROM chapter WHERE novel_id=%s", (novel,))).fetchone()
        # Pre-translation failures are retryable too; otherwise a transient TRANSLATE
        # timeout permanently strands the chapter with no prose (§6.3 recovery).
        assert row == ("error", readable, True, "saved" if readable else None)
        failure = await (await db_conn.execute("SELECT stage FROM chapter_failure WHERE novel_id=%s", (novel,))).fetchone()
        assert failure[0] == failed_stage
        if failed_stage in {"character_names", "resolve"}:
            # Prose is already durable; failed identity enrichment cannot produce a
            # partially bound graph or delay reading (§0.2, §5).
            expected = (["character_names"]
                        if failed_stage == "character_names"
                        else ["character_names", "resolve"])
            assert calls == expected
        else:
            # Graph retries retain the saved text, even if terminology has since changed.
            class SuccessfulStage(Stage):
                async def run(self, ctx, state):
                    if self.name == "translate" and readable:
                        pytest.fail("enrichment retry must not retranslate")
                    if self.name == "graph_write" and readable:
                        assert state.translation == "saved prose"
            if readable:
                monkeypatch.setattr(module, "DEFAULT_STAGES", [SuccessfulStage(n) for n in (
                    "translate", "character_names", "resolve", "state", "graph_write")])
                await worker._handle(raw)
                row = await (await db_conn.execute("SELECT status,translation_ready,enrichment_retry_at FROM chapter WHERE novel_id=%s", (novel,))).fetchone()
                assert row == ("done", True, None)
    finally:
        await delete_novel(db_conn, novel)


@pytest.mark.db
async def test_fresh_translation_yields_before_optional_enrichment(db_conn, monkeypatch):
    import pipeline.worker as module
    novel = await make_novel(db_conn)
    worker = Worker.__new__(Worker)
    worker.db, worker.cfg = db_conn, make_config()
    worker.redis = AsyncMock()
    worker.minio = worker.cache = worker.textproc = worker.embed_provider = None
    worker._provider_for_novel = AsyncMock(
        return_value=(object(), object(), "ollama", None, None, None)
    )
    worker._get_object = lambda uri: "original text"
    calls = []

    class Stage:
        def __init__(self, name): self.name = name
        async def run(self, ctx, state):
            calls.append(self.name)
            if self.name == "translate":
                state.translation = "saved prose"
                await db_conn.execute(
                    "UPDATE chapter SET translated_uri='saved' WHERE novel_id=%s", (novel,)
                )

    monkeypatch.setattr(module, "DEFAULT_STAGES", [Stage("translate"), Stage("resolve")])
    try:
        await db_conn.execute(
            "INSERT INTO chapter(novel_id,chapter_index,raw_hash,raw_uri,source_meta,status) "
            "VALUES (%s,1,'test','raw','{}','queued')", (novel,)
        )
        with pytest.raises(TranslationPublished):
            await worker._handle(message(1, novel))
        assert calls == ["translate"]
        row = await (await db_conn.execute(
            "SELECT translation_ready,translated_uri FROM chapter WHERE novel_id=%s", (novel,)
        )).fetchone()
        assert row == (True, "saved")
    finally:
        await delete_novel(db_conn, novel)


@pytest.mark.db
async def test_legacy_readable_pointer_is_demoted_before_enrichment(db_conn, monkeypatch):
    import pipeline.worker as module
    novel = await make_novel(db_conn)
    worker = Worker.__new__(Worker)
    worker.db, worker.cfg = db_conn, make_config()
    worker.redis = AsyncMock()
    worker.minio = worker.cache = worker.textproc = worker.embed_provider = None
    worker._provider_for_novel = AsyncMock(
        return_value=(object(), object(), "ollama", None, None, None)
    )
    worker._get_object = lambda uri: "saved prose" if uri == "saved" else "original text"
    calls = []

    class Stage:
        def __init__(self, name): self.name = name
        async def run(self, ctx, state): calls.append(self.name)

    monkeypatch.setattr(module, "DEFAULT_STAGES", [Stage("translate"), Stage("resolve")])
    try:
        await db_conn.execute(
            "INSERT INTO chapter(novel_id,chapter_index,raw_hash,raw_uri,source_meta,status,"
            "translation_ready,translated_uri) VALUES (%s,1,'test','raw','{}','queued',true,'saved')",
            (novel,),
        )
        with pytest.raises(TranslationPublished):
            await worker._handle(message(1, novel))
        assert calls == []
    finally:
        await delete_novel(db_conn, novel)


def test_translation_is_the_reader_critical_path_before_enrichment():
    from pipeline.stages import DEFAULT_STAGES

    names = [stage.name for stage in DEFAULT_STAGES]
    assert names[:2] == ["chunk", "translate"]
    assert names[2:] == [
        "character_names", "scan", "resolve", "display_scan", "state", "graph_write"
    ]


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
