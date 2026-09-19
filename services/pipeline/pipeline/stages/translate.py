"""Stage 4: validated glossary translation with a per-novel served-model pin."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
from contextlib import asynccontextmanager

from pipeline.batch import BatchProtocolError, BatchRequestFailed
from pipeline.context import PipelineState, StageContext, language_profile_for
from pipeline.jobs import (
    idempotency_key,
    insert_job,
    job_is_done,
    mark_job_done,
    model_for_stage,
)
from pipeline.llm.provider import BatchRequest
from pipeline.source_names import find_source_names
from pipeline.stages.chunk import chunk_text
from pipeline.term_choices import record_term_choices
from pipeline.translation import (
    GlossaryViolation,
    build_system_prompt,
    build_user_prompt,
    check_fully_translated,
    lint_translation,
    UntranslatedOutput,
    prime_glossary_terms,
    protect_glossary_terms,
    respell_names,
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


async def _glossary(db, novel_id: str, *, chapter: int) -> tuple[int, list[tuple[str, str, str]]]:
    rows = await (
        await db.execute(
            "SELECT source_term, target_term, version, deleted, constraint_class FROM glossary "
            "WHERE novel_id = %s ORDER BY source_term",
            (novel_id,),
        )
    ).fetchall()
    # Provisional terminology is stable too, but never marked human-approved (§0).
    # Includes names first seen in THIS chapter: the source-names pass records them just
    # before translation so they are primed on their first appearance, not only after.
    # A saved/tombstoned glossary entry always takes precedence.
    provisional = await (await db.execute(
        "SELECT source_term,candidates->0->>'target_term',term_role "
        "FROM character_name_review WHERE novel_id=%s AND status='pending' "
        "AND first_seen_chapter <= %s AND jsonb_array_length(candidates)>0 ORDER BY source_term",
        (novel_id, chapter),
    )).fetchall()
    known = {r[0] for r in rows}
    terms = [(r[0], r[1], r[4]) for r in rows if not r[3]]
    terms.extend((source, target, "semantic_term" if role == "semantic_term" else "character_name")
                 for source, target, role in provisional if source not in known and target)
    # The translation fingerprint includes the chosen targets, even before a glossary
    # version exists. Changing a reviewed choice cannot reuse stale translation output.
    return max((r[2] for r in rows), default=0), terms


def _translation_fingerprint(ctx: StageContext, glossary) -> str:
    """Hash every stable input included in the translation system prompt."""
    payload = [
        "translation-input-v4",
        build_system_prompt(source_lang=ctx.novel.source_lang, target_lang=ctx.novel.target_lang,
                            ontology=ctx.novel.ontology, glossary=glossary),
        ctx.novel.source_lang,
        ctx.novel.target_lang,
        ctx.novel.ontology,
        glossary,
        ctx.cfg.translation_chunk_tokens,
    ]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _translation_parts(source_text: str, ctx: StageContext) -> list[str]:
    """Split provider input without changing the source-chapter boundary.

    ``chunk_text`` preserves paragraph and sentence boundaries. Its documented escape
    hatch for a single over-budget sentence is useful for retrieval, but translation
    requests need a hard estimated ceiling, so this function subdivides only that rare
    case. The complete ordered output is reassembled before it enters storage or any
    later pipeline stage (instructions.md §3.1, §5).
    """
    budget = ctx.cfg.translation_chunk_tokens
    if budget < 1:
        raise ValueError("translation_chunk_tokens must be positive")

    max_chars = max(1, int(budget * ctx.language_profile.chars_per_token))
    parts: list[str] = []
    for chunk in chunk_text(source_text, ctx.language_profile, budget=budget):
        if len(chunk.text) <= max_chars:
            parts.append(chunk.text)
            continue
        parts.extend(
            chunk.text[start : start + max_chars]
            for start in range(0, len(chunk.text), max_chars)
        )
    return [part for part in parts if part.strip()]


async def _complete_translation(
    ctx: StageContext,
    *,
    root_key: str,
    source_text: str,
    system: str,
    requested_model: str,
) -> tuple[str, str, str]:
    """Translate ordered parts and require one consistent served identity."""
    parts = _translation_parts(source_text, ctx)
    if not parts:
        raise ValueError("cannot translate empty source text")

    request_ids = [
        root_key
        if len(parts) == 1
        else hashlib.sha256(f"{root_key}\x1fpart:{i + 1}:{len(parts)}".encode()).hexdigest()
        for i in range(len(parts))
    ]
    requests: list[BatchRequest] = []
    for index, (request_id, part) in enumerate(
        zip(request_ids, parts, strict=True), start=1
    ):
        part_system = system
        if len(parts) > 1:
            part_system += (
                f"\nThis input is contiguous part {index} of {len(parts)} of one chapter. "
                "Translate only this part; do not add a title, recap, or continuation note."
            )
        requests.append(
            {
                "id": request_id,
                "prompt": build_user_prompt(part),
                "system": part_system,
                "pin_model": True,
                "model": requested_model,
                # DeepSeek otherwise thinks at full effort and can spend the whole output
                # budget before finishing the chapter (TruncatedOutput); translation
                # doesn't need it. Other backends keep their existing default.
                **({"reasoning_effort": "none"}
                   if (getattr(ctx, "provider_id", None) or ctx.cfg.llm_provider) == "deepseek" else {}),
            }
        )
    batch_ids = await ctx.batch_manager.batch_submit_split(requests)
    results = []
    for batch_id in batch_ids:
        results.extend(await ctx.batch_manager.batch_poll(batch_id))

    expected = set(request_ids)
    unexpected = [result["id"] for result in results if result["id"] not in expected]
    if unexpected:
        raise BatchProtocolError(f"translation batch returned unexpected result ids {unexpected!r}")
    by_id = {}
    for result in results:
        request_id = result["id"]
        if request_id in by_id:
            raise BatchProtocolError(f"translation batch returned duplicate result {request_id!r}")
        by_id[request_id] = result
    missing = [request_id for request_id in request_ids if request_id not in by_id]
    if missing:
        raise BatchProtocolError(f"translation batch returned no result for {missing!r}")

    ordered = [by_id[request_id] for request_id in request_ids]
    for result in ordered:
        if result["error"] is not None:
            raise BatchRequestFailed(result["id"], result["error"])
    identities = {(result["served_provider"], result["served_model"]) for result in ordered}
    if len(identities) != 1:
        raise RuntimeError(f"translation parts changed serving identity: {sorted(identities)!r}")
    served_provider, served_model = identities.pop()
    translated = (
        ordered[0]["output"]
        if len(ordered) == 1
        else "\n\n".join(result["output"].strip() for result in ordered)
    )
    return translated, served_provider, served_model


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
            _, glossary = await _glossary(ctx.db, ctx.novel.id, chapter=state.envelope.chapter_index)
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
        if state.respell and await self._respell(ctx, state):
            return
        if ctx.novel.source_lang.split("-")[0] == "zh":
            # Names first: recorded as pending choices, then primed below like locked
            # terms, so the spelling is decided before any English exists and the reader
            # cards can find it by exact search (source_names.py).
            async with ctx.db.transaction():
                await record_term_choices(ctx, state, await find_source_names(ctx, state.envelope.raw_text))
            state.source_names_primed = True
        glossary_version, glossary = await _glossary(ctx.db, ctx.novel.id, chapter=state.envelope.chapter_index)
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

        # Locked terms are substituted into the source before the model sees it, so
        # terminology is carried through rather than recalled. validate_glossary_
        # constraints below still checks the ORIGINAL raw_text: what a term is
        # required by is the chapter as written, not the primed copy we sent.
        primed = prime_glossary_terms(state.envelope.raw_text, glossary)
        system = build_system_prompt(
            source_lang=ctx.novel.source_lang,
            target_lang=ctx.novel.target_lang,
            ontology=ctx.novel.ontology,
            glossary=glossary,
        )
        cached = await ctx.cache.get(key)
        if cached is not None:
            translated = cached
            served_provider = requested_provider
            served_model = requested_model
        else:
            translated, served_provider, served_model = await _complete_translation(
                ctx, root_key=key, source_text=primed, system=system,
                requested_model=requested_model,
            )

        # Lint before any check or write: look-alike spaces/hyphens inside names would
        # otherwise fail the glossary check and every later exact match (highlights,
        # name search) even though the name is spelled correctly.
        translated = lint_translation(translated)
        try:
            check_fully_translated(translated, ctx.novel.source_lang, ctx.novel.target_lang)
        except UntranslatedOutput as untranslated:
            # A model can stop translating partway and copy the rest through. One fresh
            # attempt with a firmer instruction; a second failure fails the chapter
            # instead of publishing half-Chinese prose as readable.
            log.warning("translate: chapter=%s left %s Chinese characters; retrying once",
                        chapter, untranslated.count)
            if cached is not None:
                await ctx.cache.delete(key)
                cached = None
            translated, served_provider, served_model = await _complete_translation(
                ctx,
                root_key=hashlib.sha256(f"{key}\x1funtranslated-retry-v1".encode()).hexdigest(),
                source_text=primed,
                system=system + "\nTranslate every sentence completely. Leave no Chinese text.",
                requested_model=requested_model,
            )
            translated = lint_translation(translated)
            check_fully_translated(translated, ctx.novel.source_lang, ctx.novel.target_lang)
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
            protected_translation, retry_provider, retry_model = await _complete_translation(
                ctx,
                root_key=protected_key,
                source_text=protect_glossary_terms(state.envelope.raw_text, glossary),
                system=build_system_prompt(
                    source_lang=ctx.novel.source_lang,
                    target_lang=ctx.novel.target_lang,
                    ontology=ctx.novel.ontology,
                    glossary=glossary,
                ) + "\nPreserve every <locked-term> element and its inner text exactly.",
                requested_model=requested_model,
            )
            retry_identity = f"{retry_provider}:{retry_model}"
            if retry_identity != translated_by:
                raise RuntimeError(
                    f"protected translation retry changed serving identity from "
                    f"{translated_by!r} to {retry_identity!r}"
                )
            try:
                protected_translation = lint_translation(strip_locked_term_tags(protected_translation))
                validate_glossary_constraints(
                    state.envelope.raw_text, protected_translation, glossary
                )
            except GlossaryViolation as retry_violation:
                # Still failing after lint and the protected retry (including malformed
                # protection markup): keep the ordinary completion, which is readable and
                # markup-free, and report only an operational count. A missed name is a
                # spelling to fix in review, not a reason to leave the chapter unreadable.
                log.warning("translate: glossary check still failing after retry chapter=%s terms=%s",
                            chapter, retry_violation.term_count)
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

    async def _respell(self, ctx: StageContext, state: PipelineState) -> bool:
        """Apply confirmed spellings to the saved translation; False means retranslate.

        No model call: the old spellings were primed, so they are in the text verbatim.
        The result still has to pass the same glossary backstop a fresh translation does.
        """
        chapter = state.envelope.chapter_index
        row = await _chapter_row(ctx.db, ctx.novel.id, chapter)
        if not row or not row[0] or not row[2] or row[2] == "external":
            return False
        glossary_version, glossary = await _glossary(ctx.db, ctx.novel.id, chapter=chapter)
        current = await asyncio.to_thread(_read_object, ctx.objects, ctx.cfg.object_bucket, row[0])
        respelled = respell_names(current, state.respell, [target for _, target, _ in glossary],
                                  ctx.novel.target_lang)
        if respelled is None:
            log.info("translate: respell chapter=%s old spelling absent; retranslating", chapter)
            return False
        try:
            validate_glossary_constraints(state.envelope.raw_text, respelled, glossary)
        except GlossaryViolation:
            log.info("translate: respell chapter=%s fails the glossary check; retranslating", chapter)
            return False
        if respelled != current:
            digest = hashlib.sha256(respelled.encode("utf-8")).hexdigest()
            uri = f"translated/{ctx.novel.id}/{chapter}/{digest}.txt"
            await asyncio.to_thread(_write_object, ctx.objects, ctx.cfg.object_bucket, uri, respelled)
            async with ctx.db.transaction():
                next_version = await (await ctx.db.execute(
                    "SELECT COALESCE(MAX(version),0)+1 FROM chapter_translation_version "
                    "WHERE novel_id=%s AND chapter_index=%s",
                    (ctx.novel.id, chapter),
                )).fetchone()
                await ctx.db.execute(
                    "INSERT INTO chapter_translation_version "
                    "(novel_id,chapter_index,version,translated_uri,translated_by,glossary_version,translation_fingerprint,reason) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,'respell')",
                    (ctx.novel.id, chapter, next_version[0], uri, row[2], glossary_version,
                     _translation_fingerprint(ctx, glossary)),
                )
                await ctx.db.execute(
                    "UPDATE chapter SET translated_uri=%s, glossary_version=%s "
                    "WHERE novel_id=%s AND chapter_index=%s",
                    (uri, glossary_version, ctx.novel.id, chapter),
                )
        log.info("translate: respelled chapter=%s without a model call", chapter)
        state.translation = respelled
        self._set_chunks(ctx, state, respelled)
        return True

    @staticmethod
    def _set_chunks(ctx: StageContext, state: PipelineState, translated: str) -> None:
        state.chunks = chunk_text(translated, language_profile_for(ctx.novel.target_lang))
