"""Coverage for the repair executor (pipeline/repair.py, migration 0043).

What matters here is not that prepare or switch work -- test_knowledge.py already covers
those -- but that the bridge between an HTTP intent and those functions preserves every
property the CLI had: one action at a time, deterministic failures are not retried
forever, backpressure is not mistaken for failure, and no gate is bypassed.
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from pathlib import Path
from unittest.mock import ANY, AsyncMock

import httpx
import pytest

from pipeline import repair
from pipeline.failures import failure_category
from pipeline.llm.provider import AdmissionRejected

from tests.conftest import DATABASE_URL
from tests.fixtures import delete_novel, make_config, make_novel


REPAIR_GO = Path(__file__).resolve().parents[3] / "services/reader-api/repair.go"


def _status_error(code: int, *, body: str = "", path: str = "/api/generate",
                  headers: dict[str, str] | None = None) -> httpx.HTTPStatusError:
    """The exception httpx itself raises for `code`, not a hand-written imitation.

    The response is constructed the same way httpx constructs provider failures, so tests
    exercise status and response metadata rather than hand-written exception messages.
    """
    response = httpx.Response(
        code,
        headers=headers,
        content=body.encode(),
        request=httpx.Request("POST", "http://127.0.0.1:11435" + path),
    )
    with pytest.raises(httpx.HTTPStatusError) as caught:
        response.raise_for_status()
    return caught.value


def test_failure_category_classifies_repair_action_errors():
    cases = [
        (_status_error(400, body='{"error":{"code":"json_validate_failed","message":"private"}}'), "provider_invalid_json"),
        (_status_error(400, body='{"error":{"code":"context_length_exceeded"}}'), "prompt_too_large"),
        (_status_error(400, body='{"error":{"code":"unsupported_parameter"}}'), "provider_bad_request"),
        (_status_error(422, body='not json'), "provider_bad_request"),
        (_status_error(413), "prompt_too_large"),
        (RuntimeError("provider output limit reached before a complete response"), "output_truncated"),
        (ValueError("review must approve the current report hash and name its reviewer"),
         "review_rejected"),
        (ValueError("novel not found"), "not_found"),
        (ValueError("requested model is not installed; no automatic download or provider fallback"),
         "model_not_installed"),
        (RuntimeError("Gemini event extraction has no configured API key"),
         "credential_missing"),
        (ValueError("installed model or inference configuration changed since snapshot"),
         "model_changed"),
        (ValueError("saved prose changed since snapshot"), "input_changed"),
        (RuntimeError("worker fenced by revision change"), "fenced"),
        (RuntimeError("Ollama exhausted num_predict; refusing incomplete output"),
         "output_truncated"),
        (TimeoutError("timed out"), "timeout"),
        (AdmissionRejected("provider unreachable: ReadTimeout"), "model_unreachable"),
        (ValueError("extraction prompt changed; create a new revision"), "model_changed"),
        (OSError("connection refused"), "model_unreachable"),
        # The literal string httpx produces when the endpoint is gone.
        (ConnectionError("All connection attempts failed"), "model_unreachable"),
        # Answered, but with its own failure -- Ollama returns this when it gives up on a
        # model load that exceeded its server-side OLLAMA_LOAD_TIMEOUT. Distinct from
        # unreachable (nothing answered) and from timeout (a budget of ours expired).
        (_status_error(500), "model_server_error"),
        (_status_error(503), "model_server_error"),
        # A provider model endpoint rejecting the requested model is distinct from a
        # missing book/revision, which remains the controlled "not found" phrase case.
        (_status_error(404), "model_not_available"),
        (KeyError("chapters"), "unknown"),
    ]
    for exc, expected in cases:
        assert failure_category(exc) == expected, exc


def test_failure_category_classifies_hosted_http_statuses_without_persisting_body():
    cases = [
        (_status_error(401, body="secret-key=must-never-be-stored"), "credential_rejected"),
        (_status_error(403, body="account credential revoked"), "credential_rejected"),
        (_status_error(404, path="/v1beta/models/gemini-2.5-flash:generateContent"),
         "model_not_available"),
        (_status_error(404, path="/health"), "not_found"),
        (_status_error(429, body='{"error":{"details":[{"retryDelay":"55s"}]}}'),
         "rate_limited"),
        (_status_error(429, body="quota exceeded for the day; resets tomorrow"),
         "quota_exhausted"),
        (_status_error(429, headers={"retry-after": "7200"}), "quota_exhausted"),
        (_status_error(500, body="provider internals must never be rendered"),
         "model_server_error"),
    ]
    for exc, expected in cases:
        assert failure_category(exc) == expected, exc


class _ProviderWaitCursor:
    def __init__(self, row):
        self.row = row

    async def fetchone(self):
        return self.row


class _ProviderWaitDB:
    def __init__(self, attempts):
        self.attempts = attempts
        self.sql = []
        self.calls = []

    async def execute(self, sql, params=None):
        self.sql.append(sql)
        self.calls.append((sql, params))
        row = (self.attempts,) if "SELECT provider_wait_attempts" in sql else None
        return _ProviderWaitCursor(row)


async def test_provider_wait_uses_bounded_exponential_backoff():
    from pipeline import event_rebuild, graph_rebuild

    rejected = AdmissionRejected("rate_limited", retry_after_s=30, category="rate_limited")
    db = _ProviderWaitDB(0)
    assert await graph_rebuild._record_provider_wait(db, "revision", rejected) is None
    update = next(params for sql,params in db.calls if "UPDATE graph_revision" in sql)
    assert update == (60, "rate_limited", 1, "revision")

    db = _ProviderWaitDB(2)
    assert await event_rebuild._record_provider_wait(db, "revision", rejected) is None
    update = next(params for sql,params in db.calls if "UPDATE event_revision" in sql)
    assert update == (240, "rate_limited", 3, "revision")


async def test_fifth_provider_rejection_stops_automatic_retries():
    from pipeline import graph_rebuild

    db = _ProviderWaitDB(4)
    rejected = AdmissionRejected("rate_limited", retry_after_s=30, category="rate_limited")
    assert await graph_rebuild._record_provider_wait(db, "revision", rejected) == "provider_retry_exhausted"
    assert any("blocked_category" in sql for sql in db.sql)


async def test_quota_exhaustion_blocks_immediately_without_wait_budget():
    from pipeline import graph_rebuild

    db = _ProviderWaitDB(0)
    rejected = AdmissionRejected("quota_exhausted", retry_after_s=86400,
                                 category="quota_exhausted")
    assert await graph_rebuild._record_provider_wait(db, "revision", rejected) == "quota_exhausted"
    assert any("blocked_category" in sql for sql in db.sql)


async def test_hosted_graph_identity_does_not_probe_local_ollama(monkeypatch):
    """Hosted re-extraction uses its pinned provider metadata, never local Ollama."""
    from pipeline import graph_rebuild

    local = AsyncMock(side_effect=AssertionError("hosted re-extraction probed Ollama"))
    monkeypatch.setattr(graph_rebuild, "local_model", local)
    monkeypatch.setattr(graph_rebuild, "graph_provider_config", AsyncMock(return_value=object()))

    class Candidate:
        async def aclose(self):
            pass

    monkeypatch.setattr(graph_rebuild, "build_provider", lambda *_args: Candidate())
    identity = await graph_rebuild.graph_model_identity(
        object(), object(), "novel", "gemini", "gemini-test")
    assert identity == {"provider": "gemini", "name": "gemini-test", "strategy": "api_two_pass",
                        "identity": {"output_tokens": 2048, "context_tokens": 32768,
                                     "schema_transport": "prompt"}}
    local.assert_not_awaited()


async def test_hosted_graph_identity_closes_candidate_when_capability_fails(monkeypatch):
    from pipeline import graph_rebuild

    monkeypatch.setattr(graph_rebuild, "graph_provider_config", AsyncMock(return_value=object()))

    class Candidate:
        closed = False

        def schema_transport(self, model):
            raise RuntimeError("capability probe failed")

        async def aclose(self):
            self.closed = True

    candidate = Candidate()
    monkeypatch.setattr(graph_rebuild, "build_provider", lambda *_args: candidate)
    with pytest.raises(RuntimeError, match="capability probe failed"):
        await graph_rebuild.graph_model_identity(object(), object(), "novel", "gemini", "gemini-test")
    assert candidate.closed


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
            ValueError("graph context exceeds hard model budget"),
            ValueError("revision cannot be rebuilt"),
            ValueError("requested model is not installed; no automatic download"),
            RuntimeError("Gemini event extraction has no configured API key"),
            RuntimeError("serving identity changed"),
            RuntimeError("worker fenced"), RuntimeError("num_predict"),
            TimeoutError("timed out"), OSError("connection refused"), KeyError("x"),
            _status_error(500),
            _status_error(400),
            _status_error(400, body='{"error":{"code":"json_validate_failed"}}'),
            # Raised by graph_rebuild/event_rebuild resume(), never by a repair action.
            RuntimeError("Ollama exhausted num_predict; refusing incomplete output"),
            _status_error(401), _status_error(403), _status_error(404),
            _status_error(429), _status_error(429, body="daily quota exceeded"),
            AdmissionRejected("provider_retry_exhausted", category="provider_retry_exhausted"),
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
        request_id = await _request(db_conn, novel,
                                    params={"provider":"gemini","model": "gemini-test",
                                            "upto_chapter": 4})

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
        assert prepared.await_args.args[3] == "gemini-test"
        assert prepared.await_args.kwargs == {"upto_chapter": 4,"provider":"gemini"}


@pytest.mark.db
async def test_extend_request_targets_the_active_revision(db_conn, monkeypatch):
    from pipeline import graph_rebuild

    async with db_conn.transaction(force_rollback=True):
        novel = await make_novel(db_conn)
        request_id = await _request(db_conn, novel, action="extend",
                                    params={"upto_chapter": 7})
        extended = AsyncMock(return_value={"revision": "r", "added": [6, 7]})
        monkeypatch.setattr(graph_rebuild, "extend", extended)

        row = await repair._claim(db_conn, novel)
        result = await repair._run(db_conn, object(), row)
        await repair._finish(db_conn, request_id, result)

        assert result["added"] == [6, 7]
        extended.assert_awaited_once_with(db_conn, ANY, novel, upto_chapter=7)


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
async def test_model_server_error_is_retried_rather_than_ending_the_request(db_conn):
    """A 5xx from the model host is infrastructure, not a verdict on this request.

    Observed live: Ollama abandons a model load that outlives its own
    OLLAMA_LOAD_TIMEOUT and answers 500. That classified as `unknown`, which is not
    transient, so a single slow load ended the rebuild outright -- no retry, and a reader
    told only that "the cause was not recognised" for something the next attempt might
    well get past.
    """
    async with db_conn.transaction(force_rollback=True):
        novel = await make_novel(db_conn)
        request_id = await _request(db_conn, novel, params={"model": "m"})
        row = await repair._claim(db_conn, novel)
        await repair._fail(db_conn, row, _status_error(500))

        state, attempts, category, retry_at = await _state(db_conn, request_id)
        assert (state, category) == ("pending", "model_server_error")
        assert retry_at is not None, "a model-server failure must be rescheduled"


class _NeverFinishes:
    """A rebuild whose chapter call never returns, the way a stuck prefill behaves."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False

    async def resume(self, db, cfg, revision_id, limit=1):
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


async def test_discard_abandons_the_chapter_in_flight(monkeypatch):
    """A discard must not queue behind the call it makes pointless.

    A chapter call can hold the worker for the whole first-token budget, and someone
    discarding a rebuild is very often doing it *because* the call looks stuck. Waiting
    for work whose only remaining purpose is to be thrown away helps nobody.
    """
    module = _NeverFinishes()
    asked = asyncio.Event()

    async def fake_watch(cfg, revision_id):
        await module.started.wait()  # Only interrupt work that actually started.
        asked.set()
        return

    monkeypatch.setattr(repair, "_watch_for_discard", fake_watch)
    async with asyncio.timeout(5):
        await repair._resume_until_discarded(None, make_config(), module, "rev-1")

    assert asked.is_set()
    assert module.cancelled, "the in-flight chapter must be cancelled, not awaited"


async def test_a_chapter_failure_still_reaches_the_caller(monkeypatch):
    """Racing the watcher must not swallow the failure the caller classifies.

    _resume_until_discarded sits between resume and the code that records a safe failure
    class, so an exception it ate would leave a rebuild stalled with an empty ledger.
    """
    class _Fails:
        async def resume(self, db, cfg, revision_id, limit=1):
            raise TimeoutError("timed out")

    never = asyncio.Event()

    async def fake_watch(cfg, revision_id):
        await never.wait()

    monkeypatch.setattr(repair, "_watch_for_discard", fake_watch)
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(5):
            await repair._resume_until_discarded(None, make_config(), _Fails(), "rev-1")


@pytest.mark.db
async def test_watch_for_discard_sees_a_real_pending_request(db_conn):
    """The watcher's own query, against real rows.

    Worth its own test because a wrong column or state here fails silently: the watcher
    would simply never fire, and the interrupt would look like it was never built.
    """
    novel = await make_novel(db_conn)
    watching = None
    try:
        revision = (await (await db_conn.execute(
            "INSERT INTO graph_revision (novel_id, ontology) VALUES (%s,'{}') RETURNING id::text",
            (novel,))).fetchone())[0]
        cfg = make_config(database_url=DATABASE_URL)

        watching = asyncio.create_task(repair._watch_for_discard(cfg, revision))
        await asyncio.sleep(0)
        assert not watching.done(), "nothing has asked for a discard yet"

        await db_conn.execute(
            "INSERT INTO repair_request (novel_id, track, action, revision_id, requested_by)"
            " VALUES (%s,'graph','discard',%s,'test')", (novel, revision))
        async with asyncio.timeout(10):
            await watching
    finally:
        if watching is not None:
            watching.cancel()
        await delete_novel(db_conn, novel)


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
async def test_prepare_starts_from_empty_after_reader_deletes_graph(db_conn, monkeypatch):
    """Deleting a graph must not strand the book with no path to a fresh revision."""
    from pipeline import graph_rebuild

    ontology = {"kinds": ["character"], "attributes": []}
    identity = {"provider": "ollama", "name": "test", "digest": "digest",
                "identity": {"num_predict": 4096}}
    monkeypatch.setattr(graph_rebuild, "local_model", AsyncMock(return_value=identity))
    monkeypatch.setattr(graph_rebuild, "discover_num_ctx", AsyncMock(return_value=16384))
    monkeypatch.setattr(graph_rebuild, "objects", lambda _cfg: None)

    async with db_conn.transaction(force_rollback=True):
        novel = await make_novel(db_conn, ontology=json.dumps(ontology))
        await db_conn.execute("UPDATE novel SET active_graph_revision=NULL WHERE id=%s", (novel,))
        await db_conn.execute("DELETE FROM graph_revision WHERE novel_id=%s", (novel,))

        rid = await graph_rebuild.prepare(db_conn, object(), novel, "test")

        row = await (await db_conn.execute(
            "SELECT state,trusted,legacy FROM graph_revision WHERE id=%s", (rid,))).fetchone()
        assert row == ("staging", False, False)
        audit = await (await db_conn.execute(
            "SELECT action,detail->>'from_empty' FROM graph_audit WHERE revision_id=%s", (rid,))).fetchone()
        assert audit == ("prepare", "true")


@pytest.mark.db
async def test_prepare_snapshots_only_the_contiguous_prefix_through_ceiling(db_conn, monkeypatch):
    from pipeline import graph_rebuild

    ontology = {"kinds": ["character"], "attributes": [], "relations": []}
    identity = {"provider": "ollama", "name": "test", "digest": "digest",
                "identity": {"num_predict": 4096}}
    monkeypatch.setattr(graph_rebuild, "local_model", AsyncMock(return_value=identity))
    monkeypatch.setattr(graph_rebuild, "discover_num_ctx", AsyncMock(return_value=16384))
    monkeypatch.setattr(graph_rebuild, "objects", lambda _cfg: None)
    monkeypatch.setattr(graph_rebuild, "read_object", lambda _client, _cfg, uri: uri)

    async with db_conn.transaction(force_rollback=True):
        novel = await make_novel(db_conn, ontology=json.dumps(ontology))
        for chapter, status in [(1, "done"), (2, "pending"), (3, "done"), (4, "done")]:
            await db_conn.execute('''INSERT INTO chapter
                (novel_id,chapter_index,raw_hash,raw_uri,translated_uri,source_meta,status,translation_ready)
                VALUES(%s,%s,%s,%s,%s,'{}',%s,%s)''',
                (novel,chapter,f"raw-{chapter}",f"raw-{chapter}",f"display-{chapter}",status,status=="done"))

        rid=await graph_rebuild.prepare(db_conn,object(),novel,"test",upto_chapter=3)
        snapshot=(await(await db_conn.execute(
            "SELECT snapshot FROM graph_revision WHERE id=%s",(rid,))).fetchone())[0]
        jobs=await(await db_conn.execute(
            "SELECT chapter_index FROM graph_job WHERE revision_id=%s ORDER BY chapter_index",(rid,))).fetchall()

        assert [c["chapter"] for c in snapshot["chapters"]] == [1]
        assert snapshot["upto_chapter"] == 3
        assert jobs == [(1,)]


@pytest.mark.db
async def test_enqueue_completed_respects_ceiling_and_stops_at_gap(db_conn, monkeypatch):
    from pipeline import graph_rebuild
    from pipeline.evidence import digest
    from psycopg.types.json import Jsonb

    monkeypatch.setattr(graph_rebuild, "objects", lambda _cfg: None)
    monkeypatch.setattr(graph_rebuild, "read_object", lambda _client, _cfg, uri: uri)
    async with db_conn.transaction(force_rollback=True):
        novel=await make_novel(db_conn,ontology='{"kinds":[],"attributes":[],"relations":[]}')
        old=(await(await db_conn.execute(
            "SELECT active_graph_revision FROM novel WHERE id=%s",(novel,))).fetchone())[0]
        await db_conn.execute("UPDATE graph_revision SET state='archived' WHERE id=%s",(old,))
        chapter_one=dict(chapter=1,raw_uri="raw-1",translated_uri="display-1",raw_hash="raw-1",
                         source_hash=digest("raw-1"),display_hash=digest("display-1"))
        model={"provider":"ollama","name":"test","digest":"digest"}
        rid=(await(await db_conn.execute('''INSERT INTO graph_revision
            (novel_id,state,trusted,legacy,ontology,model,snapshot)
            VALUES(%s,'active',true,false,'{}',%s,%s) RETURNING id::text''',
            (novel,Jsonb(model),Jsonb({"chapters":[chapter_one],"upto_chapter":3,"start_chapter":1})))).fetchone())[0]
        await db_conn.execute("UPDATE novel SET active_graph_revision=%s WHERE id=%s",(rid,novel))
        for chapter, ready in [(1, True), (2, False), (3, True), (4, True)]:
            await db_conn.execute('''INSERT INTO chapter
                (novel_id,chapter_index,raw_hash,raw_uri,translated_uri,source_meta,status,translation_ready)
                VALUES(%s,%s,%s,%s,%s,'{}','done',%s)''',
                (novel,chapter,f"raw-{chapter}",f"raw-{chapter}",f"display-{chapter}",ready))

        first=await graph_rebuild.enqueue_completed(db_conn,object(),novel)
        assert first["added"] == [] and first["first_gap"] == 2
        await db_conn.execute("UPDATE chapter SET translation_ready=true WHERE novel_id=%s AND chapter_index=2",(novel,))
        second=await graph_rebuild.enqueue_completed(db_conn,object(),novel)

        assert second["added"] == [2,3]
        snapshot=(await(await db_conn.execute("SELECT snapshot FROM graph_revision WHERE id=%s",(rid,))).fetchone())[0]
        assert [c["chapter"] for c in snapshot["chapters"]] == [1,2,3]
        assert all(c["chapter"] != 4 for c in snapshot["chapters"])


@pytest.mark.db
async def test_one_fact_exhaustive_review_is_eligible(db_conn, monkeypatch):
    from pipeline import graph_rebuild
    from pipeline.evidence import PROMPT_VERSION, digest
    from psycopg.types.json import Jsonb

    monkeypatch.setattr(graph_rebuild, "objects", lambda _cfg: None)
    monkeypatch.setattr(graph_rebuild, "read_object", lambda _client, _cfg, uri: uri)
    async with db_conn.transaction(force_rollback=True):
        novel=await make_novel(db_conn,ontology='{"kinds":["character"],"attributes":[],"relations":[]}')
        await db_conn.execute('''INSERT INTO chapter
            (novel_id,chapter_index,raw_hash,raw_uri,translated_uri,source_meta,status,translation_ready)
            VALUES(%s,1,'raw-1','raw-1','display-1','{}','done',true)''',(novel,))
        await db_conn.execute('''INSERT INTO chapter
            (novel_id,chapter_index,raw_hash,raw_uri,translated_uri,source_meta,status,translation_ready)
            VALUES(%s,2,'raw-2','raw-2','display-2','{}','done',true)''',(novel,))
        snapshot={"chapters":[dict(chapter=1,raw_uri="raw-1",translated_uri="display-1",raw_hash="raw-1",
                     source_hash=digest("raw-1"),display_hash=digest("display-1"))],
                  "glossary":[],"progress":[],"upto_chapter":1,"start_chapter":1}
        model={"provider":"ollama","name":"test","digest":"digest"}
        rid=(await(await db_conn.execute('''INSERT INTO graph_revision
            (novel_id,ontology,model,snapshot,prompt_version)
            VALUES(%s,'{"kinds":["character"]}',%s,%s,%s) RETURNING id::text''',
            (novel,Jsonb(model),Jsonb(snapshot),PROMPT_VERSION))).fetchone())[0]
        await db_conn.execute("SELECT set_config('app.graph_revision',%s,true),set_config('app.graph_generation','1',true)",(rid,))
        await db_conn.execute('''INSERT INTO graph_job
            (revision_id,chapter_index,state,input_hash,model_identity,generation)
            VALUES(%s,1,'done','h','m',1)''',(rid,))
        evidence=str(uuid.uuid4());mention=str(uuid.uuid4());entity=str(uuid.uuid4())
        await db_conn.execute('''INSERT INTO graph_evidence
            (id,revision_id,novel_id,chapter_index,source_hash,char_start,char_end,quote)
            VALUES(%s,%s,%s,1,%s,0,3,'raw')''',(evidence,rid,novel,digest("raw-1")))
        await db_conn.execute('''INSERT INTO source_mention
            (id,revision_id,novel_id,chapter_index,surface,kind,evidence_id)
            VALUES(%s,%s,%s,1,'Hero','character',%s)''',(mention,rid,novel,evidence))
        await db_conn.execute('''INSERT INTO entity
            (id,novel_id,kind,canonical,first_seen_chapter,revision_id)
            VALUES(%s,%s,'character','Hero',1,%s)''',(entity,novel,rid))
        await db_conn.execute('''INSERT INTO mention_binding
            (revision_id,mention_id,known_from_chapter,entity_id,evidence_id)
            VALUES(%s,%s,1,%s,%s)''',(rid,mention,entity,evidence))
        fact=(await(await db_conn.execute('''INSERT INTO fact
            (novel_id,entity_id,attribute,value,valid_from_chapter,source_chapter,
             revision_id,evidence_id,claim_key)
            VALUES(%s,%s,'status','awake',1,1,%s,%s,'one') RETURNING id''',
            (novel,entity,rid,evidence))).fetchone())[0]

        report=await graph_rebuild.preview(db_conn,object(),rid)
        assert report["mentions"] == [dict(id=mention,chapter=1,surface="Hero",kind="character",entity="Hero",quote="raw",
                                           target_context="display-1",entity_source="Hero",surface_target="Hero")]
        assert report["claims"][0]["target_context"] == "display-1"
        assert report["claims"][0]["value"] == "awake"
        reviewed=await graph_rebuild.record_review(db_conn,object(),rid,dict(
            review_hash=report["review_hash"],reviewer="operator",approved=True,
            known_merge_regressions=0,
            mentions=[dict(id=mention,correct=True,unambiguous=True)],
            facts=[dict(id=fact,correct=True)]))

        assert reviewed["activation_eligible"] is True
        identity = AsyncMock(return_value=model)
        monkeypatch.setattr(graph_rebuild,"graph_model_identity",identity)
        await graph_rebuild.switch(db_conn,object(),rid,reviewed["review_hash"])
        identity.assert_awaited_once_with(db_conn, ANY, novel, "ollama", "test")
        after_activation=(await(await db_conn.execute(
            "SELECT snapshot FROM graph_revision WHERE id=%s",(rid,))).fetchone())[0]
        assert [c["chapter"] for c in after_activation["chapters"]] == [1]

        result=await graph_rebuild.extend(db_conn,object(),novel,upto_chapter=2)
        after_extension=(await(await db_conn.execute(
            "SELECT snapshot FROM graph_revision WHERE id=%s",(rid,))).fetchone())[0]
        assert result["revision"] == rid
        assert result["added"] == [2]
        assert [c["chapter"] for c in after_extension["chapters"]] == [1,2]


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
async def test_graph_request_rejection_is_not_automatically_retried(db_conn):
    from pipeline import graph_rebuild

    async with db_conn.transaction(force_rollback=True):
        _, revision = await _graph_job(db_conn, attempts=1)
        delay = await graph_rebuild.record_job_failure(
            db_conn, revision, 1,
            _status_error(400, body='{"error":{"code":"json_validate_failed"}}'))
        assert delay is None
        row = await (await db_conn.execute(
            "SELECT state, category, retry_at FROM graph_job WHERE revision_id=%s",
            (revision,))).fetchone()
        assert row == ("failed", "provider_invalid_json", None)


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
async def test_graph_interruption_refunds_the_attempt_it_never_really_spent(db_conn):
    """The regression this exists for: a worker restarted while the SAME chapter
    happened to be mid-attempt, three times in a row, silently burned all three
    genuine-failure attempts on nothing but its own restarts and permanently stranded
    the revision -- 'no retries left' with no failed chapter to point an operator at."""
    from pipeline import graph_rebuild

    async with db_conn.transaction(force_rollback=True):
        _, revision = await _graph_job(db_conn, attempts=3)
        await graph_rebuild.record_job_interruption(db_conn, revision, 1)
        attempts = (await (await db_conn.execute(
            "SELECT attempts FROM graph_job WHERE revision_id=%s AND chapter_index=1",
            (revision,))).fetchone())[0]
        assert attempts == 2, "an interruption must not count against the 3-attempt budget"


@pytest.mark.db
async def test_graph_interruption_never_takes_attempts_below_zero(db_conn):
    from pipeline import graph_rebuild

    async with db_conn.transaction(force_rollback=True):
        _, revision = await _graph_job(db_conn, attempts=0)
        await graph_rebuild.record_job_interruption(db_conn, revision, 1)
        attempts = (await (await db_conn.execute(
            "SELECT attempts FROM graph_job WHERE revision_id=%s AND chapter_index=1",
            (revision,))).fetchone())[0]
        assert attempts == 0


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
async def test_a_transient_block_is_retried_after_its_cooldown(db_conn):
    """model_unreachable and timeout are the two causes worth re-probing.

    resume()'s preamble is cheap (no model call) so re-checking costs nothing if the
    endpoint is still down -- it just re-blocks with a fresh blocked_at. Without this, a
    flapping SSH tunnel to a remote Ollama left the revision stuck forever: nothing else
    ever clears blocked_at, and the repair panel has no retry action for a blocked
    revision.
    """
    async with db_conn.transaction(force_rollback=True):
        novel, revision = await _staging_revision(db_conn)
        await db_conn.execute(
            "UPDATE graph_revision SET blocked_category='model_unreachable',"
            "blocked_at=now() - interval '6 minutes' WHERE id=%s",
            (revision,))
        assert await repair._next_staging_revision(
            db_conn, "graph_revision", "graph_job", novel) == revision


@pytest.mark.db
async def test_a_non_transient_block_is_never_retried(db_conn):
    """A malformed review or a revision that fails qualified() fails identically every
    time, so unlike model_unreachable/timeout it must stay blocked no matter how long
    ago blocked_at was set."""
    async with db_conn.transaction(force_rollback=True):
        novel, revision = await _staging_revision(db_conn)
        await db_conn.execute(
            "UPDATE graph_revision SET blocked_category='model_not_installed',"
            "blocked_at=now() - interval '1 day' WHERE id=%s",
            (revision,))
        assert await repair._next_staging_revision(
            db_conn, "graph_revision", "graph_job", novel) is None


@pytest.mark.db
async def test_retry_action_clears_a_non_transient_block_immediately(db_conn):
    """The manual escape hatch for a cause _next_staging_revision never self-clears.

    An operator who has fixed the actual problem (installed the pinned model, added a
    credential) should not have to wait for a cooldown that only applies to the two
    causes assumed to heal on their own.
    """
    async with db_conn.transaction(force_rollback=True):
        novel, revision = await _staging_revision(db_conn)
        await db_conn.execute(
            "UPDATE graph_revision SET blocked_category='model_not_installed',"
            "blocked_at=now(),provider_wait_attempts=5 WHERE id=%s", (revision,))

        result = await repair._run(db_conn, None, dict(
            track="graph", action="retry", revision_id=revision, params={}))
        assert result == {"status": "retry requested", "revision": revision,
                           "was_blocked": True}

        row = await (await db_conn.execute(
            "SELECT blocked_category, blocked_at, provider_wait_attempts FROM graph_revision WHERE id=%s",
            (revision,))).fetchone()
        assert row == (None, None, 0)
        assert await repair._next_staging_revision(
            db_conn, "graph_revision", "graph_job", novel) == revision


@pytest.mark.db
async def test_retry_action_refuses_a_revision_that_is_not_a_blocked_staging_rebuild(db_conn):
    from pipeline.graph_rebuild import discard

    async with db_conn.transaction(force_rollback=True):
        _, old, rid = await _quarantined_novel(db_conn)
        await discard(db_conn, None, rid)  # archives rid, leaving no staging revision

        with pytest.raises(ValueError, match="no such staging rebuild to retry"):
            await repair._run(db_conn, None, dict(
                track="graph", action="retry", revision_id=rid, params={}))


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
