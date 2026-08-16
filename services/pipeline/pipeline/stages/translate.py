"""Stage 4: glossary-constrained translation (Phase 1.7)."""

from __future__ import annotations

import asyncio
import io
import logging

from pipeline.context import PipelineState, StageContext, language_profile_for
from pipeline.jobs import (
    idempotency_key,
    insert_job,
    job_is_done,
    mark_job_done,
    model_for_stage,
)
from pipeline.llm.provider import Class
from pipeline.stages.chunk import chunk_text
from pipeline.translation import build_system_prompt, build_user_prompt

log = logging.getLogger(__name__)

STAGE = "translate"


async def _chapter_row(db, novel_id: str, chapter: int):
    return await (
        await db.execute(
            "SELECT translated_uri, glossary_version FROM chapter "
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
    objects.put_object(bucket, key, body, length=body.getbuffer().nbytes, content_type="text/plain; charset=utf-8")


class TranslateStage:
    name = STAGE

    async def run(self, ctx: StageContext, state: PipelineState) -> None:
        if ctx.novel.source_lang == ctx.novel.target_lang:
            return

        chapter = state.envelope.chapter_index
        raw_hash = state.envelope.source_meta.raw_hash
        glossary_version, glossary = await _glossary(ctx.db, ctx.novel.id)
        key = idempotency_key(
            STAGE, raw_hash, ctx.cfg, glossary_version=glossary_version
        )
        await insert_job(
            ctx.db, novel_id=ctx.novel.id, chapter_index=chapter, stage=STAGE, key=key
        )

        row = await _chapter_row(ctx.db, ctx.novel.id, chapter)
        if await job_is_done(ctx.db, key) and row and row[0]:
            translated = await asyncio.to_thread(_read_object, ctx.objects, ctx.cfg.object_bucket, row[0])
            self._set_chunks(ctx, state, translated)
            state.translation = translated
            return

        requested_model = model_for_stage(STAGE, ctx.cfg)
        requested_pin = f"{ctx.cfg.llm_provider}:{requested_model}"
        pinned = await (
            await ctx.db.execute(
                "SELECT translation_provider FROM novel WHERE id = %s", (ctx.novel.id,)
            )
        ).fetchone()
        if pinned and pinned[0] and pinned[0] != requested_pin:
            raise RuntimeError(
                f"novel translation provider is pinned to {pinned[0]!r}; configured {requested_pin!r}"
            )

        cached = await ctx.cache.get(key)
        if cached is not None:
            translated = cached
            served_provider = ctx.cfg.llm_provider
            served_model = requested_model
        else:
            completion = await ctx.provider.complete(
                build_user_prompt(state.envelope.raw_text),
                system=build_system_prompt(
                    source_lang=ctx.novel.source_lang,
                    target_lang=ctx.novel.target_lang,
                    ontology=ctx.novel.ontology,
                    glossary=glossary,
                ),
                cls=Class.BATCH,
                pin_model=True,
                model=requested_model,
            )
            translated = completion.text
            served_provider = completion.served_provider
            served_model = completion.served_model
            await ctx.cache.put(
                key,
                translated,
                requested_model_id=requested_pin,
                served_provider=served_provider,
                served_model=served_model,
                stage=STAGE,
            )

        translated_by = f"{served_provider}:{served_model}"
        uri = f"translated/{ctx.novel.id}/{chapter}/{key}.txt"
        await asyncio.to_thread(_write_object, ctx.objects, ctx.cfg.object_bucket, uri, translated)
        async with ctx.db.transaction():
            await ctx.db.execute(
                "UPDATE novel SET translation_provider = COALESCE(translation_provider, %s) "
                "WHERE id = %s",
                (requested_pin, ctx.novel.id),
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
