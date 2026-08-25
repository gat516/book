"""Stage 4: validated glossary translation with a per-novel served-model pin."""

from __future__ import annotations

import asyncio
import io
import logging
from contextlib import asynccontextmanager

from pipeline.context import PipelineState, StageContext, language_profile_for
from pipeline.jobs import (
    idempotency_key,
    insert_job,
    job_is_done,
    mark_job_done,
    model_for_stage,
)
from pipeline.llm.provider import BatchRequest
from pipeline.stages.chunk import chunk_text
from pipeline.translation import (
    build_system_prompt,
    build_user_prompt,
    validate_glossary_constraints,
)

log = logging.getLogger(__name__)

STAGE = "translate"


async def _chapter_row(db, novel_id: str, chapter: int):
    return await (
        await db.execute(
            "SELECT translated_uri, glossary_version, translated_by FROM chapter "
            "WHERE novel_id = %s AND chapter_index = %s",
            (novel_id, chapter),
        )
    ).fetchone()


async def _glossary(db, novel_id: str) -> tuple[int, list[tuple[str, str]]]:
    rows = await (
        await db.execute(
            "SELECT source_term, target_term, version FROM glossary "
            "WHERE novel_id = %s ORDER BY source_term",
            (novel_id,),
        )
    ).fetchall()
    return (max((r[2] for r in rows), default=0), [(r[0], r[1]) for r in rows])


def _read_object(objects, bucket: str, key: str) -> str:
    response = objects.get_object(bucket, key)
    try:
        return response.read().decode("utf-8")
    finally:
        response.close()
        response.release_conn()


def _write_object(objects, bucket: str, key: str, text: str) -> None:
    body = io.BytesIO(text.encode("utf-8"))
    objects.put_object(
        bucket,
        key,
        body,
        length=body.getbuffer().nbytes,
        content_type="text/plain; charset=utf-8",
    )


async def _pinned_provider(db, novel_id: str) -> str | None:
    row = await (
        await db.execute(
            "SELECT translation_provider FROM novel WHERE id = %s", (novel_id,)
        )
    ).fetchone()
    return row[0] if row else None


@asynccontextmanager
async def _first_translation_lock(db, novel_id: str):
    """Serialize only the race that establishes a novel's first served-model pin."""
    lock_name = f"translate-provider:{novel_id}"
    await db.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (lock_name,))
    try:
        yield
    finally:
        await db.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (lock_name,))


class TranslateStage:
    name = STAGE

    async def run(self, ctx: StageContext, state: PipelineState) -> None:
        # Bootstrapped chapter (PLAN.md N5/N6): ingest-api pre-set translated_uri at
        # paste/scrape time with translated_by="external" — no translate job was ever
        # created for it, so it's distinct from the ordinary job_is_done cache-hit path
        # below (which requires a *completed* job row a bootstrapped chapter never has).
        # Skip the whole pin/provider/LLM machinery entirely; there is nothing to serve.
        chapter = state.envelope.chapter_index
        row = await _chapter_row(ctx.db, ctx.novel.id, chapter)
        if row and row[0] and row[2] == "external":
            translated = await asyncio.to_thread(
                _read_object, ctx.objects, ctx.cfg.object_bucket, row[0]
            )
            _, glossary = await _glossary(ctx.db, ctx.novel.id)
            validate_glossary_constraints(state.envelope.raw_text, translated, glossary)
            self._set_chunks(ctx, state, translated)
            state.translation = translated
            return

        if ctx.novel.source_lang == ctx.novel.target_lang:
            return

        pinned = await _pinned_provider(ctx.db, ctx.novel.id)
        if pinned is None:
            async with _first_translation_lock(ctx.db, ctx.novel.id):
                # Another worker may have established the pin while this one waited.
                pinned = await _pinned_provider(ctx.db, ctx.novel.id)
                await self._run_with_pin(ctx, state, pinned)
        else:
            await self._run_with_pin(ctx, state, pinned)

    async def _run_with_pin(
        self, ctx: StageContext, state: PipelineState, pinned: str | None
    ) -> None:
        chapter = state.envelope.chapter_index
        raw_hash = state.envelope.source_meta.raw_hash
        glossary_version, glossary = await _glossary(ctx.db, ctx.novel.id)
        if pinned is None:
            requested_provider = ctx.cfg.llm_provider
            requested_model = model_for_stage(STAGE, ctx.cfg)
            requested_id = f"{requested_provider}:{requested_model}"
        else:
            requested_provider, separator, requested_model = pinned.partition(":")
            if not separator or not requested_provider or not requested_model:
                raise RuntimeError(f"invalid translation provider pin {pinned!r}")
            if requested_provider != ctx.cfg.llm_provider:
                raise RuntimeError(
                    f"novel translation provider is pinned to {pinned!r}; "
                    f"configured provider is {ctx.cfg.llm_provider!r}"
                )
            requested_id = pinned

        key = idempotency_key(
            STAGE,
            raw_hash,
            ctx.cfg,
            glossary_version=glossary_version,
            model_id=requested_id,
        )

        row = await _chapter_row(ctx.db, ctx.novel.id, chapter)
        if await job_is_done(ctx.db, key) and row and row[0]:
            translated = await asyncio.to_thread(
                _read_object, ctx.objects, ctx.cfg.object_bucket, row[0]
            )
            validate_glossary_constraints(state.envelope.raw_text, translated, glossary)
            self._set_chunks(ctx, state, translated)
            state.translation = translated
            return

        cached = await ctx.cache.get(key)
        if cached is not None:
            translated = cached
            served_provider = requested_provider
            served_model = requested_model
        else:
            request: BatchRequest = {
                "id": key,
                "prompt": build_user_prompt(state.envelope.raw_text),
                "system": build_system_prompt(
                    source_lang=ctx.novel.source_lang,
                    target_lang=ctx.novel.target_lang,
                    ontology=ctx.novel.ontology,
                    glossary=glossary,
                ),
                "pin_model": True,
                "model": requested_model,
            }
            batch_id = await ctx.batch_manager.batch_submit([request])
            results = await ctx.batch_manager.batch_poll(batch_id)
            result = ctx.batch_manager.require_single_result(key, results)
            translated = result["output"]
            served_provider = result["served_provider"]
            served_model = result["served_model"]

        translated_by = f"{served_provider}:{served_model}"
        if not served_provider or not served_model:
            raise RuntimeError("translation provider returned an empty served identity")
        if pinned is not None and translated_by != pinned:
            raise RuntimeError(
                f"translation was pinned to {pinned!r}, but {translated_by!r} served it"
            )
        if pinned is None and translated_by != requested_id:
            key = idempotency_key(
                STAGE,
                raw_hash,
                ctx.cfg,
                glossary_version=glossary_version,
                model_id=translated_by,
            )

        validate_glossary_constraints(state.envelope.raw_text, translated, glossary)
        await insert_job(
            ctx.db, novel_id=ctx.novel.id, chapter_index=chapter, stage=STAGE, key=key
        )
        if cached is None:
            await ctx.cache.put(
                key,
                translated,
                requested_model_id=translated_by,
                served_provider=served_provider,
                served_model=served_model,
                stage=STAGE,
            )

        uri = f"translated/{ctx.novel.id}/{chapter}/{key}.txt"
        await asyncio.to_thread(
            _write_object, ctx.objects, ctx.cfg.object_bucket, uri, translated
        )
        async with ctx.db.transaction():
            if pinned is None:
                await ctx.db.execute(
                    "UPDATE novel SET translation_provider = %s "
                    "WHERE id = %s AND translation_provider IS NULL",
                    (translated_by, ctx.novel.id),
                )
            await ctx.db.execute(
                "UPDATE chapter SET translated_uri = %s, translated_by = %s, glossary_version = %s "
                "WHERE novel_id = %s AND chapter_index = %s",
                (uri, translated_by, glossary_version, ctx.novel.id, chapter),
            )
            await mark_job_done(ctx.db, key)

        state.translation = translated
        self._set_chunks(ctx, state, translated)

    @staticmethod
    def _set_chunks(ctx: StageContext, state: PipelineState, translated: str) -> None:
        state.chunks = chunk_text(translated, language_profile_for(ctx.novel.target_lang))
