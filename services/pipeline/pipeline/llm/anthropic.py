"""Anthropic backend — the quality bar for extraction/resolution (instructions.md §5.4).

Uses the official ``anthropic`` SDK (the provider module is the one place a vendor SDK
may be imported — stages never do, §5.4). Completion goes through the Messages API;
embeddings raise, because Anthropic has no embeddings endpoint — the embedding backend
is separate and always routes to Ollama's ``nomic-embed-text`` (§5.4). Native Batch API
(``client.messages.batches``) is deferred to Milestone 3 (§6.2); until then the default
``SequentialBatchMixin`` stands in.

``served_model`` is read from the API response's own ``model`` field, not echoed back
from the request — Anthropic resolves date-suffix-free aliases (e.g. ``claude-haiku-4-5``)
to a concrete snapshot, so the response value is the first genuinely independent "what
answered" signal in this codebase. Cache token counts come from ``usage`` and matter now
for cost accounting (PLAN workstream B) even before any gateway is involved; a future
gateway backend needs the same fields to do cache-aware rate-limit reservation (gateway
spec D4).
"""

from __future__ import annotations

import anthropic

from pipeline.llm.provider import Class, Completion, SequentialBatchMixin


class AnthropicProvider(SequentialBatchMixin):
    def __init__(self, *, model: str, max_tokens: int = 8192) -> None:
        super().__init__()
        self._model = model
        self._max_tokens = max_tokens
        # Reads ANTHROPIC_API_KEY from the environment (§10).
        self._client = anthropic.AsyncAnthropic()

    async def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        json_mode: bool = False,
        cls: Class = Class.BATCH,
        pin_model: bool = False,
        model: str | None = None,
    ) -> Completion:
        # No router sits in front of this backend, so pin_model has nothing to forbid
        # yet; accepted for interface parity with a future gateway backend (§14.3).
        # json_mode is best-effort here: the extraction prompts already ask for JSON and
        # the pipeline validates with pydantic. Structured outputs (output_config.format)
        # can be wired in later if free-form JSON proves unreliable.
        kwargs: dict = {
            "model": model or self._model,
            "max_tokens": self._max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            kwargs["system"] = system

        resp = await self._client.messages.create(**kwargs)
        text = "".join(block.text for block in resp.content if block.type == "text")
        usage = resp.usage
        return Completion(
            text=text,
            served_provider="anthropic",
            served_model=resp.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        )

    async def embed(self, texts: list[str], *, cls: Class = Class.BATCH) -> list[list[float]]:
        raise NotImplementedError(
            "Anthropic has no embeddings API; use the Ollama embed backend (§5.4)."
        )

    async def aclose(self) -> None:
        await self._client.close()
