"""Stage 4: validated glossary translation with a per-novel served-model pin."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
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
    GlossaryViolation,
    build_system_prompt,
    build_user_prompt,
    prime_glossary_terms,
    protect_glossary_terms,
    strip_locked_term_tags,
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


async def _glossary(db, novel_id: str) -> tuple[int, list[tuple[str, str, str]]]:
    rows = await (
        await db.execute(
            "SELECT source_term, target_term, version, deleted, constraint_class FROM glossary "
            "WHERE novel_id = %s ORDER BY source_term",
            (novel_id,),
        )
    ).fetchall()
    # Tombstones still advance the cache version, including deletion of the last term.
    return (max((r[2] for r in rows), default=0), [(r[0], r[1], r[4]) for r in rows if not r[3]])


def _translation_fingerprint(ctx: StageContext, glossary) -> str:
    """Hash every stable input included in the translation system prompt."""
    payload = [
        "translation-input-v3",
        build_system_prompt(source_lang=ctx.novel.source_lang, target_lang=ctx.novel.target_lang,
                            ontology=ctx.novel.ontology, glossary=glossary),
        ctx.novel.source_lang,
        ctx.novel.target_lang,
        ctx.novel.ontology,
        glossary,
    ]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


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
        translation_fingerprint = _translation_fingerprint(ctx, glossary)
        # The provider actually resolved for this novel this chapter (PLAN.md Phase N4:
        # its own novel_provider_config.provider if it has one, else the process-wide
        # cfg.llm_provider) — NOT ctx.cfg.llm_provider directly, which would ignore a
        # per-novel override entirely and could wrongly reject a novel pinned to a
        # provider that differs from the process default.
        resolved_provider = ctx.provider_id or ctx.cfg.llm_provider
        if pinned is None:
            requested_provider = resolved_provider
            requested_model = model_for_stage(STAGE, ctx.cfg, ctx.model_override)
            requested_id = f"{requested_provider}:{requested_model}"
        else:
            requested_provider, separator, requested_model = pinned.partition(":")
            if not separator or not requested_provider or not requested_model:
                raise RuntimeError(f"invalid translation provider pin {pinned!r}")
            if requested_provider != resolved_provider:
                raise RuntimeError(
                    f"novel translation provider is pinned to {pinned!r}; "
                    f"configured provider is {resolved_provider!r}"
                )
            requested_id = pinned

        key = idempotency_key(
            STAGE,
            raw_hash,
            ctx.cfg,
            glossary_version=translation_fingerprint,
            model_id=requested_id,
        )

        row = await _chapter_row(ctx.db, ctx.novel.id, chapter)
        if await job_is_done(
            ctx.db,
            novel_id=ctx.novel.id,
            chapter_index=chapter,
            stage=STAGE,
            key=key,
        ) and row and row[0]:
            translated = await asyncio.to_thread(
                _read_object, ctx.objects, ctx.cfg.object_bucket, row[0]
            )
            self._set_chunks(ctx, state, translated)
            state.translation = translated
            return

        cached = await ctx.cache.get(key)
        if cached is not None:
            translated = cached
            served_provider = requested_provider
            served_model = requested_model
        else:
            # Locked terms are substituted into the source before the model sees it, so
            # terminology is carried through rather than recalled. validate_glossary_
            # constraints below still checks the ORIGINAL raw_text: what a term is
            # required by is the chapter as written, not the primed copy we sent.
            primed = prime_glossary_terms(state.envelope.raw_text, glossary)
            request: BatchRequest = {
                "id": key,
                "prompt": build_user_prompt(primed),
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
                glossary_version=translation_fingerprint,
                model_id=translated_by,
            )

        warning_count = 0
        try:
            validate_glossary_constraints(state.envelope.raw_text, translated, glossary)
        except GlossaryViolation as first_violation:
            if cached is not None:
                await ctx.cache.delete(key)
            if not first_violation.recoverable:
                raise
            protected_key = hashlib.sha256(f"{key}\x1fprotected-term-retry-v1".encode()).hexdigest()
            protected_request: BatchRequest = {
                "id": protected_key,
                "prompt": build_user_prompt(
                    protect_glossary_terms(state.envelope.raw_text, glossary)
                ),
                "system": build_system_prompt(
                    source_lang=ctx.novel.source_lang,
                    target_lang=ctx.novel.target_lang,
                    ontology=ctx.novel.ontology,
                    glossary=glossary,
                ) + "\nPreserve every <locked-term> element and its inner text exactly.",
                "pin_model": True,
                "model": requested_model,
            }
            retry_batch = await ctx.batch_manager.batch_submit([protected_request])
            retry_results = await ctx.batch_manager.batch_poll(retry_batch)
            retry = ctx.batch_manager.require_single_result(protected_key, retry_results)
            retry_identity = f"{retry['served_provider']}:{retry['served_model']}"
            if retry_identity != translated_by:
                raise RuntimeError(
                    f"protected translation retry changed serving identity from "
                    f"{translated_by!r} to {retry_identity!r}"
                )
            protected_translation = strip_locked_term_tags(retry["output"])
            try:
                validate_glossary_constraints(
                    state.envelope.raw_text, protected_translation, glossary
                )
            except GlossaryViolation as retry_violation:
                if not retry_violation.recoverable:
                    raise
                if first_violation.hard or retry_violation.hard:
                    # Character-name spellings are identity-bearing terminology. Never
                    # publish a replacement that omits or retranslates one.
                    raise retry_violation
                # The ordinary completion is readable and contains no protection markup.
                # Preserve it, report only an operational count, and let enrichment run.
                warning_count = max(first_violation.term_count, retry_violation.term_count, 1)
            else:
                translated = protected_translation

        await insert_job(
            ctx.db, novel_id=ctx.novel.id, chapter_index=chapter, stage=STAGE, key=key
        )
        if cached is None and warning_count == 0:
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
            next_version = await (await ctx.db.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM chapter_translation_version "
                "WHERE novel_id=%s AND chapter_index=%s",
                (ctx.novel.id, chapter),
            )).fetchone()
            await ctx.db.execute(
                "INSERT INTO chapter_translation_version "
                "(novel_id,chapter_index,version,translated_uri,translated_by,glossary_version,translation_fingerprint,reason) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                (ctx.novel.id, chapter, next_version[0], uri, translated_by,
                 glossary_version, translation_fingerprint,
                 "name-repair" if next_version[0] > 1 else "initial"),
            )
            await ctx.db.execute(
                "UPDATE chapter SET translated_uri = %s, translated_by = %s, glossary_version = %s, "
                "translation_warning_code = %s, translation_warning_count = %s "
                "WHERE novel_id = %s AND chapter_index = %s",
                (
                    uri,
                    translated_by,
                    glossary_version,
                    "locked_terms_missing" if warning_count else None,
                    warning_count,
                    ctx.novel.id,
                    chapter,
                ),
            )
            await mark_job_done(
                ctx.db,
                novel_id=ctx.novel.id,
                chapter_index=chapter,
                stage=STAGE,
                key=key,
            )

        state.translation = translated
        self._set_chunks(ctx, state, translated)

    @staticmethod
    def _set_chunks(ctx: StageContext, state: PipelineState, translated: str) -> None:
        state.chunks = chunk_text(translated, language_profile_for(ctx.novel.target_lang))
