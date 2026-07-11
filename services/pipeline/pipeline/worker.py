"""The offline worker drain loop (instructions.md §5; PLAN.md §1.1).

Drains the Redis ``jobs:pending`` queue that ``ingest-api`` fills, loads each chapter's
body from the object store, and runs it through the stage list. This phase the stages are
no-op stubs, so a chapter's whole journey is: pop → load → run stubs → mark ``done``.

Queue discipline (§0.7): ``BLMOVE jobs:pending jobs:processing RIGHT LEFT`` *moves* the
pointer to a processing list rather than plain-popping it, so a crash mid-chapter leaves
the pointer recoverable instead of lost. A recovery sweep over ``jobs:processing`` is
future work (noted, not built).
"""

from __future__ import annotations

import asyncio
import logging

import psycopg
import redis.asyncio as aredis
from minio import Minio

from pipeline.config import Config
from pipeline.context import LanguageProfile, NovelMeta, PipelineState, StageContext
from pipeline.envelope import ChapterEnvelope, QueueMessage, SourceMeta
from pipeline.llm import provider_from_env
from pipeline.stages import DEFAULT_STAGES

log = logging.getLogger(__name__)

PENDING_QUEUE = "jobs:pending"  # keep in sync with ingest-api/store.go:16
PROCESSING_QUEUE = "jobs:processing"


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
        self.db: psycopg.AsyncConnection | None = None

    async def start(self) -> None:
        # autocommit: each status update lands immediately; the skeleton has no
        # multi-statement transactions yet (graph-write introduces those in 1.4).
        self.db = await psycopg.AsyncConnection.connect(
            self.cfg.database_url, autocommit=True
        )
        log.info("worker connected; draining %s", PENDING_QUEUE)
        try:
            await self._loop()
        finally:
            await self.db.close()

    async def _loop(self) -> None:
        while True:
            raw = await self.redis.blmove(
                PENDING_QUEUE, PROCESSING_QUEUE, self.cfg.queue_timeout, "RIGHT", "LEFT"
            )
            if raw is None:
                continue  # timed out with an empty queue; poll again
            try:
                await self._handle(raw)
            except Exception:  # noqa: BLE001 — never let one poisoned chapter kill the loop
                log.exception("chapter processing failed; leaving pointer in %s", PROCESSING_QUEUE)
                continue
            # Success: drop the processing pointer for this message.
            await self.redis.lrem(PROCESSING_QUEUE, 1, raw)

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
            language_profile=LanguageProfile(lang=source_lang),
            provider=self.provider,
            db=self.db,
            objects=self.minio,
            cfg=self.cfg,
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
