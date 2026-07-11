"""Anthropic backend — the quality bar for extraction/resolution (instructions.md §5.4).

Uses the official ``anthropic`` SDK (the provider module is the one place a vendor SDK
may be imported — stages never do, §5.4). Completion goes through the Messages API;
embeddings raise, because Anthropic has no embeddings endpoint — the embedding backend
is separate and always routes to Ollama's ``nomic-embed-text`` (§5.4). Native Batch API
(``client.messages.batches``) is deferred to Milestone 3 (§6.2); until then the default
``SequentialBatchMixin`` stands in.
"""

from __future__ import annotations

import anthropic

from pipeline.llm.provider import SequentialBatchMixin


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
    ) -> str:
        # json_mode is best-effort here: the extraction prompts already ask for JSON and
        # the pipeline validates with pydantic. Structured outputs (output_config.format)
        # can be wired in later if free-form JSON proves unreliable.
        kwargs: dict = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            kwargs["system"] = system

        resp = await self._client.messages.create(**kwargs)
        return "".join(block.text for block in resp.content if block.type == "text")

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError(
            "Anthropic has no embeddings API; use the Ollama embed backend (§5.4)."
        )

    async def aclose(self) -> None:
        await self._client.close()
