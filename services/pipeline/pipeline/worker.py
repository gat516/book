"""The offline worker drain loop (instructions.md §5, §6.3; PLAN.md §1.1).

Claims chapters atomically in chapter order (with explicit reading priority), renews
their leases during inference, and recovers abandoned claims. Translation readiness
is durable and independent of optional graph enrichment. See queue.py for the Redis
contract; database completion markers still protect append-only graph writes.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from contextlib import suppress

import psycopg
import redis.asyncio as aredis
from minio import Minio

from pipeline.batch import BatchManager
from pipeline.cache import LLMCache
from pipeline.config import Config
from pipeline.context import NovelMeta, PipelineState, StageContext, language_profile_for
from pipeline.envelope import ChapterEnvelope, QueueMessage, SourceMeta
from pipeline.llm import embed_provider_from_env, provider_from_env
from pipeline.llm.provider import AdmissionRejected, LLMProvider
from pipeline.provider_config import (
    build_names_provider,
    build_provider,
    build_resolve_provider,
    resolve_provider_config,
)
from pipeline import queue
from pipeline.failures import error_code, failure_category, record_failure
from pipeline.records_publish import publish_records
from pipeline.stages import DEFAULT_STAGES
from pipeline.stages.translate import TranslateStage
from pipeline.textproc import textproc_from_config
from pipeline.inference_runtime import coordinated_provider

log = logging.getLogger(__name__)

PENDING_QUEUE = "jobs:pending"  # keep in sync with ingest-api/store.go:16
PROCESSING_QUEUE = "jobs:processing"
PROCESSING_STARTED = "jobs:processing:started"  # HASH: raw queue message -> claim epoch
# HASH: raw queue message -> the stage currently running for it. Purely observational —
# nothing recovers from it and the reaper ignores it — but without it a chapter in flight
# is a black box: a single TRANSLATE call on a local model can run for minutes with no
# outward sign, which is indistinguishable from a hung worker. reader-api surfaces this.
PROCESSING_STAGE = "jobs:processing:stage"
PROCESSING_STAGE_STARTED = "jobs:processing:stage:started"
PUBLISH_STAGE = """
redis.call('HSET', KEYS[1], ARGV[1], ARGV[2])
redis.call('HSET', KEYS[2], ARGV[1], ARGV[3])
return 1
"""
WORKER_HEARTBEAT = "jobs:worker:heartbeat"
# Longer than the claim-renewal interval (at most 30s), so a healthy worker stays online
# in the reader even while one slow model request owns the event loop's useful work.
WORKER_HEARTBEAT_TTL_SECONDS = 45


class ChapterFailed(Exception):
    """A chapter failed and the failure is already recorded on its row (status='error').

    Distinguishing this from an arbitrary crash matters for queue hygiene. The reaper
    exists to recover jobs whose worker died mid-flight, so a claim is normally left in
    jobs:processing for it to find. But a chapter that failed cleanly is not lost — the
    outcome is durably recorded — and leaving its claim behind has two bad effects: it
    shows up as phantom in-flight work for a whole visibility_timeout, and the reaper then
    requeues it to fail again on the next sweep, forever, burning model time on a chapter
    that fails deterministically.
    """


class ChapterDiscarded(Exception):
    """Operator discarded this chapter's graph attempt; do not retry automatically."""


class NovelDeleted(Exception):
    """The chapter's owner disappeared; cancel work without retry or failure history."""


class TranslationPublished(Exception):
    """Validated prose is durable; hand remaining work back as low-priority enrichment."""


class ProviderRetryExhausted(Exception):
    """Five consecutive provider admissions were rejected for one chapter."""

    category = "provider_retry_exhausted"


NOVEL_CHECK_SECONDS = 2
TRANSLATE_STAGE = "translate"
# Partial translation text, so a reader watching an in-progress chapter sees it arrive
# rather than staring at a spinner for minutes. Keyed per chapter and short-lived: it is a
# view of work in flight, never a source of record — the finished translation goes to the
# object store like always.
PREVIEW_KEY = "translate:preview:{novel_id}:{chapter_index}"
PREVIEW_TTL_SECONDS = 900
# Write at most this often. A token-rate write would hammer Redis for no visible benefit;
# prose arriving twice a second already reads as live.
PREVIEW_THROTTLE_SECONDS = 0.5
# A chapter gets at most five generic stage attempts, including its initial ingest. The
# counter is durable so a worker crash or Redis claim release cannot reset the bound.
MAX_ENRICHMENT_ATTEMPTS = 5
# Provider admission has its own durable streak. Keep this separate from the
# generic enrichment-attempt cap so a future change to one policy cannot silently
# change the other (§0 append-only retry state).
MAX_PROVIDER_RETRY_ATTEMPTS = 5
PROVIDER_RETRY_BASE_SECONDS = 60.0
GENERIC_RETRY_BASE_SECONDS = 300.0
GENERIC_RETRY_CAP_SECONDS = 3600.0


def _set_stream_sink(provider, sink) -> None:
    """Attach a partial-output observer if this provider supports one.

    Only some backends can stream (OllamaProvider today), and the LLMProvider protocol
    deliberately says nothing about it — so this is a capability check, not an assumption.
    """
    if hasattr(provider, "stream_sink"):
        provider.stream_sink = sink


class Worker:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.redis = aredis.from_url(cfg.redis_url, decode_responses=True)
        self.minio = Minio(
            cfg.object_endpoint,
            access_key=cfg.object_access_key,
            secret_key=cfg.object_secret_key,
            secure=cfg.object_secure,
        )
        # _default_provider/_default_batch_manager back every novel with no
        # novel_provider_config row of its own (PLAN.md Phase N4's zero-config backward
        # compat) — renamed from the old self.provider/self.batch_manager, which every
        # chapter used unconditionally regardless of novel.
        self._default_provider = coordinated_provider(
            provider_from_env(cfg), self.redis, provider_id=cfg.llm_provider,
            base_url=(cfg.groq_base_url if cfg.llm_provider == 'groq' else
                      cfg.deepseek_base_url if cfg.llm_provider == 'deepseek' else ''))
        self._default_batch_manager = BatchManager(self._default_provider)
        self.embed_provider = embed_provider_from_env(cfg)
        # Per-novel (provider, batch manager, identity, stage-specific Ollama clients,
        # model override) cache, keyed by novel_id — a
        # provider wraps a live httpx/SDK client, so this must be built once and reused
        # across chapters, not reconstructed per chapter (PLAN.md Phase N4).
        self._provider_cache: dict[
            str,
            tuple[LLMProvider, BatchManager, str, LLMProvider | None, LLMProvider | None, str | None],
        ] = {}
        self.textproc = textproc_from_config(
            cfg.textproc_backend, cfg.textproc_grpc_addr, cfg.textproc_timeout_seconds
        )
        # Same Redis connection as the job queue: both are this service's own state.
        # A future gateway keeps its quota state in a SEPARATE logical database (§15.5)
        # — a backfill filling this one must not evict the limiter's accounting.
        self.cache = LLMCache(self.redis)
        self.db: psycopg.AsyncConnection | None = None
        self.stopping = asyncio.Event()
        # Consecutive admission deferrals across chapters. A rate limit is a property of
        # the account, not the chapter, so backing off per-chapter would just round-robin
        # the same saturated quota. Reset by any completed chapter.
        self._deferrals = 0

    def request_stop(self) -> None:
        log.info("shutdown requested; cancelling current work at a resumable boundary")
        self.stopping.set()

    async def _run_until_stopping(self, awaitable):
        """Run one unit of work, cancelling it when shutdown is requested.

        Stage and repair code owns its rollback/retry cleanup. Awaiting the cancelled
        task here is load-bearing: it lets provider calls release admission locks and
        graph rebuilds mark their chapter immediately retryable before the process exits
        (§0 append-only/idempotent ingestion).
        """
        work = asyncio.create_task(awaitable)
        stop = asyncio.create_task(self.stopping.wait())
        try:
            done, _ = await asyncio.wait((work, stop), return_when=asyncio.FIRST_COMPLETED)
            # Work that completed concurrently with the signal wins. This avoids
            # requeueing an output that was already committed at the boundary.
            if work in done:
                return await work
            # request_stop() may set the event immediately before the work coroutine
            # completes its watcher cleanup. Give that cleanup a short bounded grace
            # window before treating the signal as cancellation; this preserves the
            # completed-work boundary without weakening cancellation of live work.
            await asyncio.wait({work}, timeout=0.01)
            if work.done():
                return await work
            work.cancel()
            return await work
        finally:
            for task in (work, stop):
                if not task.done():
                    task.cancel()
            await asyncio.gather(work, stop, return_exceptions=True)

    async def start(self) -> None:
        if self.stopping.is_set():
            return
        # autocommit for chapter-status updates outside graph-write; graph-write itself
        # (1.4) opens its own explicit transaction per chapter via GraphWriter.
        self.db = await psycopg.AsyncConnection.connect(
            self.cfg.database_url, autocommit=True
        )
        log.info("worker connected; draining %s", PENDING_QUEUE)
        try:
            # Process liveness must not depend on the work loop reaching its next
            # iteration. Hosted calls and provider-directed backoff can both outlive the
            # heartbeat TTL while the worker is healthy.
            await asyncio.gather(
                self._loop(), self._reap_forever(), self._heartbeat_forever()
            )
        finally:
            await self.textproc.aclose()
            await self.db.close()

    async def _loop(self) -> None:
        while not self.stopping.is_set():
            await self.redis.set(WORKER_HEARTBEAT, str(time.time()), ex=WORKER_HEARTBEAT_TTL_SECONDS)
            claimed_at = str(time.time())
            raw = await self.redis.eval(queue.CLAIM, len(queue.KEYS), *queue.KEYS, claimed_at)
            if raw is None:
                try:
                    await self._run_until_stopping(self._drain_background())
                except asyncio.CancelledError:
                    if not self.stopping.is_set():
                        raise
                    log.info("shutdown cancelled background work; resumable state recorded")
                    break
                except AdmissionRejected as exc:
                    log.info("provider admission deferred category=%s retry_after_s=%.1f",
                             getattr(exc, "category", "rate_limited"), exc.retry_after_s)
                    await self._idle(max(exc.retry_after_s, 0.25))
                except Exception:
                    log.exception("revision enrichment failed; retained for explicit resume")
                await self._idle(min(max(self.cfg.queue_timeout, 0.1), 1))
                continue
            heartbeat = asyncio.create_task(self._renew_claim(raw, claimed_at))
            disposition = "done"
            enrichment_raw = ""
            try:
                await self._run_until_stopping(self._handle_claim(raw))
            except asyncio.CancelledError:
                if not self.stopping.is_set():
                    raise
                # RELEASE atomically returns the pointer to pending only after provider,
                # transaction and preview cleanup in _handle_claim has completed.
                disposition = "retry"
                log.info("shutdown cancelled chapter work; returning claim to pending")
            except NovelDeleted:
                msg = QueueMessage.model_validate_json(raw)
                await self._clear_preview(msg.novel_id, msg.chapter_index)
                log.info("novel deleted; cancelled chapter %s/%s", msg.novel_id, msg.chapter_index)
            except TranslationPublished:
                msg = QueueMessage.model_validate_json(raw)
                disposition = "enrich"
                enrichment_raw = QueueMessage(
                    novel_id=msg.novel_id,
                    chapter_index=msg.chapter_index,
                    enrichment=True,
                ).model_dump_json()
                log.info(
                    "chapter %s/%s released for reading; enrichment requeued behind translations",
                    msg.novel_id,
                    msg.chapter_index,
                )
            except AdmissionRejected as exc:
                # Provider backpressure is durable chapter state, not a reason to hold
                # a Redis claim or sleep for minutes. Release the claim now; the reaper's
                # due-retry sweep will enqueue it after provider_retry_at.
                msg = QueueMessage.model_validate_json(raw)
                if getattr(exc, "record_generation_id", None):
                    msg.record_generation_id = exc.record_generation_id
                await self._record_provider_rejection(
                    msg, exc
                )
                log.info(
                    "provider admission deferred durably category=%s retry_after_s=%.1f",
                    getattr(exc, "category", "rate_limited"), exc.retry_after_s,
                )
            except ChapterDiscarded:
                log.info("chapter graph attempt discarded; dropping claim")
            except ChapterFailed:
                # The outcome is recorded on the chapter row, so this job is not lost and
                # must not be resurrected: drop the claim outright. Leaving it made failed
                # chapters appear as in-flight work and had the reaper retry them on every
                # sweep — re-running a translation that fails the same way each time.
                log.exception("chapter processing failed; recorded and dropping claim")
            except Exception:  # noqa: BLE001 — never let one poisoned chapter kill the loop
                # Failed BEFORE the outcome could be recorded (e.g. the chapter row or
                # object store was unreachable), so this one really is unfinished business:
                # leave the claim for the reaper to requeue.
                log.exception("chapter processing failed; leaving pointer in %s", PROCESSING_QUEUE)
                disposition = "abandoned"
                await self.redis.hdel(PROCESSING_STAGE, raw)
                await self.redis.hdel(PROCESSING_STAGE_STARTED, raw)
            finally:
                heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat
            if disposition != "abandoned":
                await self.redis.eval(queue.RELEASE, len(queue.KEYS), *queue.KEYS,
                                      raw, claimed_at, disposition, enrichment_raw)

    async def _drain_background(self) -> None:
        """Nothing runs below chapter work any more.

        The graph/event rebuild lifecycles and the repair queue they served were retired
        with the legacy graph: a records generation is rebuilt by re-enriching chapters
        in order, which is ordinary chapter work and goes through the normal queue.
        """
        return

    async def _handle_claim(self, raw: str) -> None:
        msg = QueueMessage.model_validate_json(raw)
        # Embeddings are an optional retrieval index. They must never gate the durable
        # translation/records/display path: records_publish() stores NULL for a failed
        # or unavailable vector and retrieval simply excludes that row (§0 append-only).
        work = asyncio.create_task(self._handle(raw))
        owner = asyncio.create_task(self._watch_novel(msg.novel_id))
        try:
            done, _ = await asyncio.wait((work, owner), return_when=asyncio.FIRST_COMPLETED)
            if owner in done:
                await owner  # NovelDeleted takes precedence over a racing FK failure.
            await work
        finally:
            # Await cancellation before releasing the claim: HTTP cancellation unwinds
            # Ollama admission, stage transactions and the translation preview observer.
            for task in (work, owner):
                if not task.done():
                    task.cancel()
            await asyncio.gather(work, owner, return_exceptions=True)

    async def _watch_novel(self, novel_id: str) -> None:
        # Use a separate autocommit connection: the stage connection may be busy or
        # inside a transaction. Only a committed deletion is a cancellation signal.
        # Checking Postgres also works if best-effort Redis cleanup failed (§0, §6.3).
        while True:
            try:
                async with await psycopg.AsyncConnection.connect(
                    self.cfg.database_url, autocommit=True, connect_timeout=5
                ) as monitor:
                    while True:
                        async with asyncio.timeout(5):
                            row = await (await monitor.execute(
                                "SELECT 1 FROM novel WHERE id=%s", (novel_id,)
                            )).fetchone()
                        if row is None:
                            raise NovelDeleted(novel_id)
                        await asyncio.sleep(NOVEL_CHECK_SECONDS)
            except (psycopg.Error, TimeoutError):
                # A database outage is not evidence of deletion. Keep the claim and
                # heartbeat intact and reconnect without interrupting live inference.
                log.warning("could not check novel existence; retrying", exc_info=True)
                await asyncio.sleep(NOVEL_CHECK_SECONDS)

    async def _idle(self, seconds: float) -> None:
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.stopping.wait(), timeout=seconds)

    async def _heartbeat_forever(self) -> None:
        """Renew process liveness independently of claims and model calls.

        Process-wide on purpose. A graph rebuild's inference calls can run well past
        WORKER_HEARTBEAT_TTL_SECONDS with no chapter claim around to renew it
        (_renew_claim only runs for jobs:pending work), and provider-directed backoff
        can outlast it again while the worker is perfectly healthy. Renewing here covers
        both, and the idle gaps between them, without any path having to remember to
        start a renewer of its own.
        """
        interval = max(1.0, WORKER_HEARTBEAT_TTL_SECONDS / 3)
        while not self.stopping.is_set():
            try:
                await self.redis.set(
                    WORKER_HEARTBEAT,
                    str(time.time()),
                    ex=WORKER_HEARTBEAT_TTL_SECONDS,
                )
            except Exception:  # Redis recovery belongs to the owning work loops.
                log.exception("could not renew worker heartbeat")
            if self.stopping.is_set():
                break
            await self._idle(interval)

    async def _renew_claim(self, raw: str, claimed_at: str) -> None:
        # Total chapter duration is not evidence of a dead worker. Keep the original
        # start time for the UI, and renew a separate lease during slow model calls.
        interval = max(0.1, min(30, self.cfg.visibility_timeout / 3))
        while True:
            await asyncio.sleep(interval)
            try:
                await self.redis.set(
                    WORKER_HEARTBEAT, str(time.time()), ex=WORKER_HEARTBEAT_TTL_SECONDS
                )
                renewed = await self.redis.eval(queue.RENEW, len(queue.KEYS), *queue.KEYS,
                                                raw, claimed_at, str(time.time()))
                if not renewed:
                    return
            except Exception:
                log.exception("could not renew chapter claim")

    async def _reap_forever(self) -> None:
        """Requeue jobs claimed longer than ``visibility_timeout`` ago (§6.3).

        A job re-appearing in jobs:pending while a crashed worker's claim record still
        exists is the failure mode this guards: without this loop, ``jobs:processing``
        plus its claim hash grow forever and no stranded chapter is ever retried.
        """
        while not self.stopping.is_set():
            await self._idle(self.cfg.reaper_interval)
            if self.stopping.is_set():
                break
            try:
                await self._reap_once()
                await self._retry_enrichment()
            except Exception:  # noqa: BLE001 — a reaper crash must not kill the worker
                log.exception("reaper sweep failed")

    async def _reap_once(self) -> None:
        started: dict[str, str] = await self.redis.hgetall(PROCESSING_STARTED)
        now = time.time()
        for raw, started_at in started.items():
            if now - float(started_at) < self.cfg.visibility_timeout:
                continue
            # Check the latest heartbeat and recover atomically, so a renewal racing
            # this sweep cannot requeue a chapter that is still actively running.
            removed = await self.redis.eval(queue.REAP, len(queue.KEYS), *queue.KEYS,
                                            raw, str(now), self.cfg.visibility_timeout)
            if removed:
                log.warning(
                    "reaper requeued stranded job after %.0fs: %r",
                    now - float(started_at),
                    raw,
                )

    async def _handle(self, raw: str) -> None:
        msg = QueueMessage.model_validate_json(raw)
        assert self.db is not None

        chapter = await self._fetch_one(
            "SELECT raw_hash, raw_uri, source_meta, status, translation_ready, translated_uri, enrichment_discarded FROM chapter"
            " WHERE novel_id = %s AND chapter_index = %s",
            (msg.novel_id, msg.chapter_index),
        )
        if chapter is None:
            log.warning("no chapter row for %s/%s; dropping", msg.novel_id, msg.chapter_index)
            await self._clear_preview(msg.novel_id, msg.chapter_index)
            return
        raw_hash, raw_uri, source_meta, _status, readable, translated_uri = chapter[:6]
        # Keep lightweight worker fakes and older deployments compatible while the
        # forward migration rolls out; real rows always include the durable flag.
        discarded = bool(chapter[6]) if len(chapter) > 6 else False
        if msg.enrichment and discarded:
            log.info("chapter %s/%s graph work was discarded; dropping pointer", msg.novel_id, msg.chapter_index)
            return
        # A durable translation can legitimately carry a later enrichment pointer. The
        # enrichment flag explicitly bypasses this stale-pointer guard; only an ordinary
        # duplicate or retranslate pointer is safe to drop.
        if _status == "done" and not msg.retranslate and not msg.enrichment:
            log.info("chapter %s/%s already done; dropping stale pointer", msg.novel_id, msg.chapter_index)
            return
        # Priority/retranslate pointers are explicit operator/manual retries. They start
        # fresh durable retry streaks; scheduler-generated enrichment pointers leave the
        # provider streak intact.
        if msg.priority or msg.retranslate:
            await self._reset_provider_retry(msg)
            await self.db.execute(
                "UPDATE chapter SET enrichment_attempts=0,enrichment_retry_at=NULL,"
                "enrichment_retry_generation_id=NULL "
                "WHERE novel_id=%s AND chapter_index=%s",
                (msg.novel_id, msg.chapter_index),
            )

        novel = await self._fetch_one(
            "SELECT source_lang, target_lang, ontology FROM novel WHERE id = %s",
            (msg.novel_id,),
        )
        if novel is None:
            log.warning("no novel row for %s; dropping", msg.novel_id)
            await self._clear_preview(msg.novel_id, msg.chapter_index)
            return
        source_lang, target_lang, ontology = novel
        if msg.record_generation_id:
            # Rebuild/retry pointers are generation-fenced at dequeue time as well as
            # inside prepare_generation. Dropping a pointer for a discarded rebuild is
            # safe; retrying it against the current generation would mix identities.
            active_generation = await self._fetch_one(
                "SELECT active_record_generation::text FROM novel WHERE id=%s",
                (msg.novel_id,),
            )
            if active_generation is None or str(active_generation[0]) != msg.record_generation_id:
                log.warning(
                    "dropping stale records pointer for %s/%s: expected generation %s, active %s",
                    msg.novel_id, msg.chapter_index, msg.record_generation_id,
                    active_generation[0] if active_generation else None,
                )
                return
        (provider, batch_manager, provider_id, names_provider, resolve_provider,
         model_override) = await self._provider_for_novel(msg.novel_id)

        raw_text = await asyncio.to_thread(self._get_object, raw_uri)

        envelope = ChapterEnvelope(
            novel_id=msg.novel_id,
            chapter_index=msg.chapter_index,
            raw_text=raw_text,
            source_lang=source_lang,
            source_meta=SourceMeta.model_validate(source_meta),
        )
        ctx = StageContext(
            novel=NovelMeta(
                id=msg.novel_id,
                source_lang=source_lang,
                target_lang=target_lang,
                ontology=ontology,
            ),
            language_profile=language_profile_for(source_lang),
            provider=provider,
            batch_manager=batch_manager,
            # Embeddings are an independent retrieval backend. Even gateway completion
            # routing must not replace an explicitly selected EMBED_PROVIDER.
            embed_provider=self.embed_provider,
            db=self.db,
            objects=self.minio,
            cfg=self.cfg,
            cache=self.cache,
            textproc=self.textproc,
            provider_id=provider_id,
            names_provider=names_provider,
            resolve_provider=resolve_provider,
            model_override=model_override,
        )
        state = PipelineState(
            envelope=envelope,
            expected_record_generation_id=msg.record_generation_id,
        )
        stage_name = ""
        try:
            for stage in DEFAULT_STAGES:
                if msg.enrichment and stage.name in {"records", "display_scan"}:
                    discarded_row = await self._fetch_one(
                        "SELECT enrichment_discarded FROM chapter WHERE novel_id=%s AND chapter_index=%s",
                        (msg.novel_id, msg.chapter_index),
                    )
                    if discarded_row and discarded_row[0]:
                        raise ChapterDiscarded()
                stage_name = stage.name
                # Publish the stage name and its own start time atomically. The claim's
                # original timestamp remains the total-chapter clock; conflating the two
                # made the UI attribute every earlier stage's minutes to the current one.
                await self.redis.eval(
                    PUBLISH_STAGE,
                    2,
                    PROCESSING_STAGE,
                    PROCESSING_STAGE_STARTED,
                    raw,
                    stage.name,
                    str(time.time()),
                )
                # Stream partial output for the one stage that produces prose. Scoped to
                # translate deliberately: RESOLVE and STATE emit JSON, and feeding that to
                # a reader-facing preview would be noise.
                streaming = stage.name == TRANSLATE_STAGE
                if streaming:
                    _set_stream_sink(provider, self._preview_sink(msg.novel_id, msg.chapter_index))
                try:
                    if stage.name == TRANSLATE_STAGE and readable and not msg.retranslate:
                        if translated_uri:
                            state.translation = await asyncio.to_thread(self._get_object, translated_uri)
                            TranslateStage._set_chunks(ctx, state, state.translation)
                        if not msg.enrichment:
                            # Legacy/recovered pointers may predate the explicit
                            # enrichment flag even though their prose is already durable.
                            # Normalize them at the same atomic queue boundary instead of
                            # letting old graph work block a new translation.
                            raise TranslationPublished
                    else:
                        try:
                            await stage.run(ctx, state)
                        except AdmissionRejected:
                            raise
                        except Exception:
                            raise
                    if stage.name == TRANSLATE_STAGE and (not readable or msg.retranslate):
                        # The translation stage has validated and durably saved the text.
                        # Unknown facts and later enrichment errors cannot revoke it.
                        await self.db.execute(
                            "UPDATE chapter SET translation_ready=true WHERE novel_id=%s AND chapter_index=%s",
                            (msg.novel_id, msg.chapter_index))
                        readable = True
                        log.info("chapter %s/%s translation ready", msg.novel_id, msg.chapter_index)
                        await self._clear_preview(msg.novel_id, msg.chapter_index)
                        # A provider call has now made durable progress. Do not carry a
                        # pre-translation admission streak into optional enrichment.
                        await self._reset_provider_retry(msg)
                        if not msg.enrichment:
                            # Do not spend the reader queue's claim on optional graph work.
                            # RELEASE atomically swaps this pointer for enrichment=True,
                            # whose scheduler rank is below every untranslated chapter.
                            raise TranslationPublished
                        log.info(
                            "chapter %s/%s translation already readable; enriching graph",
                            msg.novel_id,
                            msg.chapter_index,
                        )
                finally:
                    if streaming:
                        _set_stream_sink(provider, None)
            # Records publication owns the generation/run transaction and chunk rebuild.
            # It is intentionally after translation so an untranslated chapter can become
            # readable early and enrichment is retried as a low-priority queue message.
            if state.records is not None:
                if msg.enrichment:
                    discarded_row = await self._fetch_one(
                        "SELECT enrichment_discarded FROM chapter WHERE novel_id=%s AND chapter_index=%s",
                        (msg.novel_id, msg.chapter_index),
                    )
                    if discarded_row and discarded_row[0]:
                        raise ChapterDiscarded()
                stage_name = "publish"
                await self.redis.eval(
                    PUBLISH_STAGE,
                    2,
                    PROCESSING_STAGE,
                    PROCESSING_STAGE_STARTED,
                    raw,
                    stage_name,
                    str(time.time()),
                )
                await publish_records(ctx, state)
        except TranslationPublished:
            raise
        except AdmissionRejected as exc:
            # The outer loop requeues without turning capacity pressure into a job error.
            # Preserve the generation selected by RECORDS so the outer claim handler can
            # persist it with provider backoff; the raw queue pointer may predate the pin.
            if state.record_generation_id and not getattr(exc, "record_generation_id", None):
                exc.record_generation_id = state.record_generation_id
            raise
        except Exception as exc:
            # Deletion can win the race with a stage write before the watcher polls.
            # There is no surviving chapter on which to record a failure or retry.
            if await self._fetch_one("SELECT 1 FROM novel WHERE id=%s", (msg.novel_id,)) is None:
                raise NovelDeleted(msg.novel_id) from exc
            async with self.db.transaction():
                await record_failure(self.db, msg.novel_id, msg.chapter_index, stage_name, exc)
                if stage_name in {"records", "publish"}:
                    # The run row is the reader-facing progress surface for this chapter.
                    # Without this it stays 'processing' forever and a failed extraction or
                    # publication is indistinguishable from a slow one. Only the bounded
                    # category from failures.py is stored -- never the exception text
                    # (§0, migration 0046).
                    run_generation = state.record_generation_id
                    if run_generation:
                        await self.db.execute(
                            "UPDATE record_run SET status='failed',"
                            "diagnostics=jsonb_build_object('failure',%s::text) "
                            "WHERE novel_id=%s AND generation_id=%s AND chapter_index=%s "
                            "AND status='processing'",
                            (error_code(exc), msg.novel_id, run_generation, msg.chapter_index))
                await self._set_status(msg, "name_repair_error" if msg.retranslate else "error")
                # Schedule a retry for ANY recorded failure. This used to be guarded by
                # `if readable`, which silently made every pre-TRANSLATE failure terminal:
                # CHARACTER_NAMES runs second of eight, so a transient model timeout there
                # left the chapter at status='error' with a NULL enrichment_retry_at, which
                # _retry_enrichment's `enrichment_retry_at <= now()` could never match.
                # Bounded attempts still prevent a deterministic failure from monopolizing
                # the model. Increment only here, when a retry is actually scheduled:
                # the successful translation -> enrichment handoff is not itself a failed
                # attempt (§0 append-only retry state).
                retry_row = await (await self.db.execute(
                    "SELECT enrichment_attempts FROM chapter "
                    "WHERE novel_id=%s AND chapter_index=%s FOR UPDATE",
                    (msg.novel_id, msg.chapter_index),
                )).fetchone()
                enrichment_attempts = int(retry_row[0]) if retry_row else 0
                next_attempt = enrichment_attempts + 1
                retry_generation = state.record_generation_id or msg.record_generation_id
                await self.db.execute(
                    "UPDATE chapter SET enrichment_attempts=%s,"
                    "enrichment_retry_at = CASE WHEN %s < %s "
                    "THEN now() + (%s * interval '1 second') ELSE NULL END,"
                    "enrichment_retry_generation_id=CASE WHEN %s < %s THEN %s::uuid ELSE NULL END,"
                    "provider_retry_attempts=0,provider_retry_at=NULL,"
                    "provider_retry_category=NULL,provider_retry_generation_id=NULL "
                    "WHERE novel_id=%s AND chapter_index=%s AND enrichment_attempts < %s",
                    (next_attempt,
                     next_attempt, MAX_ENRICHMENT_ATTEMPTS,
                     min(GENERIC_RETRY_BASE_SECONDS * (2 ** max(next_attempt - 1, 0)),
                         GENERIC_RETRY_CAP_SECONDS),
                     next_attempt, MAX_ENRICHMENT_ATTEMPTS, retry_generation,
                     msg.novel_id, msg.chapter_index, MAX_ENRICHMENT_ATTEMPTS))
            # Re-raise as ChapterFailed so the drain loop knows the outcome was recorded
            # and the claim can be dropped rather than left for the reaper to retry.
            raise ChapterFailed(f"chapter {msg.chapter_index} failed") from exc
        self._deferrals = 0
        await self._set_status(msg, "done")
        await self.db.execute(
            "UPDATE chapter SET translation_ready=true, enrichment_retry_at=NULL,"
            "enrichment_attempts=0,enrichment_retry_generation_id=NULL "
            "WHERE novel_id=%s AND chapter_index=%s", (msg.novel_id, msg.chapter_index))
        await self._reset_provider_retry(msg)
        # The chapter is readable from the object store now, so the in-flight preview would
        # only ever be a stale, partial copy of it.
        await self._clear_preview(msg.novel_id, msg.chapter_index)
        log.info("chapter %s/%s done", msg.novel_id, msg.chapter_index)

    async def _retry_enrichment(self) -> None:
        assert self.db is not None
        # Keep the due timestamp until the work succeeds. Queue insertion is atomic and
        # deduplicated; a crash between database inspection and enqueue cannot strand it.
        rows = await (await self.db.execute(
            # No translation_ready filter: a chapter that failed BEFORE translate is
            # exactly the one with nothing durable saved, so it is the most important to
            # retry, not the one to skip.
            "SELECT c.novel_id::text, c.chapter_index, c.status, "
            "COALESCE(c.enrichment_retry_generation_id::text, "
            "c.provider_retry_generation_id::text) FROM chapter c "
            # needs_name_review is included because nothing produces it any more: the
            # character-name gate no longer blocks translation, so a chapter still parked
            # at that status is stranded exactly the way pre-TRANSLATE failures were before
            # 0033. It was excluded then precisely because it WAS a live human gate.
            "WHERE NOT c.enrichment_discarded AND ((enrichment_retry_at <= now() AND enrichment_attempts < %s) "
            "OR (provider_retry_at <= now() AND provider_retry_attempts < %s)) "
            "ORDER BY LEAST(COALESCE(enrichment_retry_at, 'infinity'::timestamptz), "
            "COALESCE(provider_retry_at, 'infinity'::timestamptz)) LIMIT 20",
            (MAX_ENRICHMENT_ATTEMPTS, MAX_PROVIDER_RETRY_ATTEMPTS),
        )).fetchall()
        for novel_id, chapter, status, generation_id in rows:
            msg = QueueMessage(novel_id=novel_id, chapter_index=chapter, enrichment=True,
                               retranslate=status == "name_repair_error",
                               record_generation_id=generation_id)
            await self.redis.eval(queue.ENQUEUE_ENRICHMENT, len(queue.KEYS), *queue.KEYS,
                                  novel_id, chapter, msg.model_dump_json())

    async def _record_provider_rejection(self, msg: QueueMessage, exc: AdmissionRejected) -> None:
        """Persist provider backoff and release this claim without sleeping."""
        assert self.db is not None
        category = failure_category(exc)
        async with self.db.transaction():
            row = await (await self.db.execute(
                "SELECT provider_retry_attempts,translation_ready FROM chapter "
                "WHERE novel_id=%s AND chapter_index=%s FOR UPDATE",
                (msg.novel_id, msg.chapter_index),
            )).fetchone()
            if row is None:
                return
            attempt = int(row[0]) + 1
            if attempt >= MAX_PROVIDER_RETRY_ATTEMPTS:
                await self.db.execute(
                    "UPDATE chapter SET provider_retry_attempts=%s,provider_retry_at=NULL,"
                    "provider_retry_category=%s,provider_retry_generation_id=NULL,"
                    "status=CASE WHEN translation_ready THEN status ELSE 'error' END "
                    "WHERE novel_id=%s AND chapter_index=%s",
                    (attempt, category, msg.novel_id, msg.chapter_index),
                )
                await record_failure(
                    self.db, msg.novel_id, msg.chapter_index, "provider",
                    ProviderRetryExhausted(),
                )
                return
            delay = max(
                PROVIDER_RETRY_BASE_SECONDS * (2 ** (attempt - 1)),
                max(float(exc.retry_after_s), 0.0),
            )
            await self.db.execute(
                "UPDATE chapter SET provider_retry_attempts=%s,"
                "provider_retry_at=now() + (%s * interval '1 second'),"
                "provider_retry_category=%s,provider_retry_generation_id=%s "
                "WHERE novel_id=%s AND chapter_index=%s",
                (attempt, delay, category, getattr(msg, "record_generation_id", None),
                 msg.novel_id, msg.chapter_index),
            )

    async def _reset_provider_retry(self, msg: QueueMessage) -> None:
        await self.db.execute(
            "UPDATE chapter SET provider_retry_attempts=0,provider_retry_at=NULL,"
            "provider_retry_category=NULL,provider_retry_generation_id=NULL "
            "WHERE novel_id=%s AND chapter_index=%s",
            (msg.novel_id, msg.chapter_index),
        )

    def _preview_sink(self, novel_id: str, chapter_index: int):
        """Build a throttled writer for one chapter's in-progress translation."""
        key = PREVIEW_KEY.format(novel_id=novel_id, chapter_index=chapter_index)
        last_write = 0.0

        async def sink(text: str) -> None:
            nonlocal last_write
            now = time.monotonic()
            if now - last_write < PREVIEW_THROTTLE_SECONDS:
                return
            last_write = now
            await self.redis.set(key, text, ex=PREVIEW_TTL_SECONDS)

        return sink

    async def _clear_preview(self, novel_id: str, chapter_index: int) -> None:
        await self.redis.delete(PREVIEW_KEY.format(novel_id=novel_id, chapter_index=chapter_index))

    async def _provider_for_novel(
        self, novel_id: str
    ) -> tuple[
        LLMProvider, BatchManager, str, LLMProvider | None, LLMProvider | None, dict[str, str] | None
    ]:
        """Return (provider, batch_manager, provider_id) for novel_id, memoized for the
        life of the process (PLAN.md Phase N4). A novel with no novel_provider_config row
        gets the process-wide default; the cache holds that too, so this is still one
        lookup per novel rather than one per chapter.
        """
        cached = self._provider_cache.get(novel_id)
        if cached is not None:
            return cached
        if self.cfg.llm_provider == "gateway":
            provider = provider_from_env(self.cfg, tenant=novel_id)
            # The gateway owns its own admission and deadlines; a second direct-to-Ollama
            # client would bypass exactly the scheduling it exists to provide (§14).
            result = (provider, BatchManager(provider), self.cfg.gateway_provider, None, None, None)
            self._provider_cache[novel_id] = result
            return result
        # Merges the novel's own row over the account-wide credential (migration 0035),
        # so one key in Settings serves every book while a book may still override it.
        row = await resolve_provider_config(self.db, novel_id, self.cfg.llm_provider)
        if row is None:
            result = (
                self._default_provider,
                self._default_batch_manager,
                self.cfg.llm_provider,
                build_names_provider(self.cfg, provider_id=self.cfg.llm_provider),
                build_resolve_provider(self.cfg, provider_id=self.cfg.llm_provider),
                None,
            )
        else:
            provider = coordinated_provider(
                build_provider(row, self.cfg), self.redis, provider_id=row.provider,
                base_url=row.base_url or '')
            result = (
                provider,
                BatchManager(provider),
                row.provider,
                build_names_provider(self.cfg, provider_id=row.provider, row=row),
                build_resolve_provider(self.cfg, provider_id=row.provider, row=row),
                {"translate": row.translate_model or row.model or self.cfg.llm_model_translate,
                 "extract": row.extract_model or row.model or self.cfg.llm_model_extract},
            )
        self._provider_cache[novel_id] = result
        return result

    async def _fetch_one(self, sql: str, params: tuple):
        async with self.db.cursor() as cur:  # type: ignore[union-attr]
            await cur.execute(sql, params)
            return await cur.fetchone()

    async def _set_status(self, msg: QueueMessage, status: str) -> None:
        await self.db.execute(  # type: ignore[union-attr]
            "UPDATE chapter SET status = %s WHERE novel_id = %s AND chapter_index = %s",
            (status, msg.novel_id, msg.chapter_index),
        )

    def _get_object(self, key: str) -> str:
        resp = self.minio.get_object(self.cfg.object_bucket, key)
        try:
            return resp.read().decode("utf-8")
        finally:
            resp.close()
            resp.release_conn()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    async def run() -> None:
        worker = Worker(Config.load())
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, worker.request_stop)
        await worker.start()
    asyncio.run(run())


if __name__ == "__main__":
    main()
