"""Per-call name-stage resume boundaries (§0, §6.1), never approved terminology."""
from contextlib import asynccontextmanager
import hashlib
import json
import logging

from psycopg.pq import TransactionStatus

from pipeline.llm.provider import Completion

log = logging.getLogger(__name__)


@asynccontextmanager
async def uncached_completion(ctx, prompt, **kwargs):
    yield await (ctx.names_provider or ctx.provider).complete(prompt, **kwargs)


class NameCheckpoints:
    def __init__(self, ctx, state):
        self.chapter = state.envelope.chapter_index
        self.source_hash = hashlib.sha256(state.envelope.raw_text.encode()).hexdigest()

    @asynccontextmanager
    async def __call__(self, ctx, prompt, **kwargs):
        # The worker uses autocommit. Refuse an outer transaction whose rollback could
        # erase successful calls after a later admission rejection (§6.1).
        if not ctx.db.autocommit or ctx.db.info.transaction_status != TransactionStatus.IDLE:
            raise RuntimeError("name checkpoints require an idle autocommit connection")
        provider = ctx.provider_id or ctx.cfg.llm_provider
        request = dict(kwargs, cls=kwargs["cls"].value)
        identity = ["character-names-v1", self.source_hash, ctx.novel.source_lang,
                    ctx.novel.target_lang, provider, ctx.cfg.prompt_version,
                    ctx.cfg.config_version, prompt, request]
        key = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False,
                                      separators=(",", ":")).encode()).hexdigest()
        row = await (await ctx.db.execute(
            "SELECT response,served_provider,served_model FROM character_name_checkpoint "
            "WHERE novel_id=%s AND chapter_index=%s AND request_key=%s",
            (ctx.novel.id, self.chapter, key))).fetchone()
        if row:
            log.info("character_names: resumed saved call chapter=%d key=%s", self.chapter, key[:12])
            yield Completion(text=row[0], served_provider=row[1], served_model=row[2])
            return
        completion = await (ctx.names_provider or ctx.provider).complete(prompt, **kwargs)
        # Save only after the caller's validation succeeds. Focused-review validation
        # failures keep their existing conservative fallback and never poison retries.
        yield completion
        # This is a request-scoped checkpoint, not a claim that the requested model
        # served the output. Preserve actual attribution even after failover (§14.3).
        await ctx.db.execute(
            """INSERT INTO character_name_checkpoint
              (novel_id,chapter_index,request_key,source_hash,requested_provider,
               requested_model,served_provider,served_model,response)
              VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
            (ctx.novel.id, self.chapter, key, self.source_hash, provider, kwargs["model"],
             completion.served_provider, completion.served_model, completion.text))
