"""The offline worker drain loop (instructions.md §5, §6.3; PLAN.md §1.1).

Drains the Redis ``jobs:pending`` queue that ``ingest-api`` fills, loads each chapter's
body from the object store, and runs it through the stage list. This phase the stages are
no-op stubs, so a chapter's whole journey is: pop → load → run stubs → mark ``done``.

Queue discipline (§0.7, §6.3): ``BLMOVE jobs:pending jobs:processing RIGHT LEFT`` *moves*
the pointer to a processing list rather than plain-popping it, so a crash mid-chapter
leaves the pointer recoverable instead of lost — but only if something actually recovers
it. Recording a claim timestamp and running a reaper sweep is what makes that true: on
claim, ``jobs:processing:started[<raw message>] = now()``; a background reaper re-queues
any claim older than ``cfg.visibility_timeout`` back onto ``jobs:pending``. Keyed by the
raw queue message itself (matching the existing ``LREM`` pattern below) rather than a
``job_id`` — this ``QueueMessage`` doesn't carry one yet; spec §3.4 anticipates a richer
message shape that would make ``job_id`` the natural key instead. Without the reaper, a
crashed worker's chapter is stranded in ``jobs:processing`` forever — the pointer is
"recoverable" only in the sense that nothing stops you from recovering it by hand.
"""

from __future__ import annotations

import asyncio
import logging
import time

import psycopg
import redis.asyncio as aredis
from minio import Minio

from pipeline.cache import LLMCache
from pipeline.config import Config
from pipeline.context import NovelMeta, PipelineState, StageContext, language_profile_for
from pipeline.envelope import ChapterEnvelope, QueueMessage, SourceMeta
from pipeline.llm import embed_provider_from_env, provider_from_env
from pipeline.stages import DEFAULT_STAGES

log = logging.getLogger(__name__)

PENDING_QUEUE = "jobs:pending"  # keep in sync with ingest-api/store.go:16
PROCESSING_QUEUE = "jobs:processing"
PROCESSING_STARTED = "jobs:processing:started"  # HASH: raw queue message -> claim epoch


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
        self.provider = provider_from_env(cfg)
        self.embed_provider = embed_provider_from_env(cfg)
        # Same Redis connection as the job queue: both are this service's own state.
        # A future gateway keeps its quota state in a SEPARATE logical database (§15.5)
        # — a backfill filling this one must not evict the limiter's accounting.
        self.cache = LLMCache(self.redis)
        self.db: psycopg.AsyncConnection | None = None

    async def _assert_embed_dim(self) -> None:
        """Fail fast at startup, not hundreds of chunks into a run (§10).

        A dimension mismatch between EMBED_DIM and what the model actually returns is
        otherwise a raw psycopg error on the first chunk insert of the first chapter —
        this makes it a clear assertion before any work starts.
        """
        [vec] = await self.embed_provider.embed(["dimension probe"])
        if len(vec) != self.cfg.embed_dim:
            raise RuntimeError(
                f"embed model {self.cfg.embed_model!r} returned {len(vec)} dims, "
                f"but EMBED_DIM={self.cfg.embed_dim} (must match chunk/entity.embedding, see 0004)"
            )

    async def start(self) -> None:
        await self._assert_embed_dim()
        # autocommit for chapter-status updates outside graph-write; graph-write itself
        # (1.4) opens its own explicit transaction per chapter via GraphWriter.
        self.db = await psycopg.AsyncConnection.connect(
            self.cfg.database_url, autocommit=True
        )
        log.info("worker connected; draining %s", PENDING_QUEUE)
        try:
            await asyncio.gather(self._loop(), self._reap_forever())
        finally:
            await self.db.close()

    async def _loop(self) -> None:
        while True:
            raw = await self.redis.blmove(
                PENDING_QUEUE, PROCESSING_QUEUE, self.cfg.queue_timeout, "RIGHT", "LEFT"
            )
            if raw is None:
                continue  # timed out with an empty queue; poll again
            # Claim timestamp for the reaper (§6.3): this write must land before any
            # await that could crash the process, or the reaper can't find a claim it
            # doesn't know exists yet.
            await self.redis.hset(PROCESSING_STARTED, raw, time.time())
            try:
                await self._handle(raw)
            except Exception:  # noqa: BLE001 — never let one poisoned chapter kill the loop
                log.exception("chapter processing failed; leaving pointer in %s", PROCESSING_QUEUE)
                continue
            # Success: drop the processing pointer and its claim record for this message.
            await self.redis.lrem(PROCESSING_QUEUE, 1, raw)
            await self.redis.hdel(PROCESSING_STARTED, raw)

    async def _reap_forever(self) -> None:
        """Requeue jobs claimed longer than ``visibility_timeout`` ago (§6.3).

        A job re-appearing in jobs:pending while a crashed worker's claim record still
        exists is the failure mode this guards: without this loop, ``jobs:processing``
        plus its claim hash grow forever and no stranded chapter is ever retried.
        """
        while True:
            await asyncio.sleep(self.cfg.reaper_interval)
            try:
                await self._reap_once()
            except Exception:  # noqa: BLE001 — a reaper crash must not kill the worker
                log.exception("reaper sweep failed")

    async def _reap_once(self) -> None:
        started: dict[str, str] = await self.redis.hgetall(PROCESSING_STARTED)
        now = time.time()
        for raw, started_at in started.items():
            if now - float(started_at) < self.cfg.visibility_timeout:
                continue
            # Only reclaim if the pointer is still actually in jobs:processing — a
            # completed job that raced this sweep before its own HDEL lands here safely,
            # since LREM on an absent value is a no-op and we still clear the stale hash
            # field either way.
            removed = await self.redis.lrem(PROCESSING_QUEUE, 1, raw)
            await self.redis.hdel(PROCESSING_STARTED, raw)
            if removed:
                await self.redis.lpush(PENDING_QUEUE, raw)
                log.warning(
                    "reaper requeued stranded job after %.0fs: %r",
                    now - float(started_at),
                    raw,
                )

    async def _handle(self, raw: str) -> None:
        msg = QueueMessage.model_validate_json(raw)
        assert self.db is not None

        chapter = await self._fetch_one(
            "SELECT raw_hash, raw_uri, source_meta, status FROM chapter"
            " WHERE novel_id = %s AND chapter_index = %s",
            (msg.novel_id, msg.chapter_index),
        )
        if chapter is None:
            log.warning("no chapter row for %s/%s; dropping", msg.novel_id, msg.chapter_index)
            return
        raw_hash, raw_uri, source_meta, _status = chapter

        novel = await self._fetch_one(
            "SELECT source_lang, target_lang, ontology FROM novel WHERE id = %s",
            (msg.novel_id,),
        )
        if novel is None:
            log.warning("no novel row for %s; dropping", msg.novel_id)
            return
        source_lang, target_lang, ontology = novel

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
            provider=self.provider,
            embed_provider=self.embed_provider,
            db=self.db,
            objects=self.minio,
            cfg=self.cfg,
            cache=self.cache,
        )
        state = PipelineState(envelope=envelope)

        try:
            for stage in DEFAULT_STAGES:
                await stage.run(ctx, state)
        except Exception:
            await self._set_status(msg, "error")
            raise
        await self._set_status(msg, "done")
        log.info("chapter %s/%s done", msg.novel_id, msg.chapter_index)

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
    asyncio.run(Worker(Config.load()).start())


if __name__ == "__main__":
    main()
