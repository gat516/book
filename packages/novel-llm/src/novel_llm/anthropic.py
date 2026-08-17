"""Anthropic direct provider."""

from __future__ import annotations

import anthropic

from novel_llm.provider import Class, Completion, SequentialBatchMixin


class AnthropicProvider(SequentialBatchMixin):
    def __init__(self, *, model: str, max_tokens: int = 8192) -> None:
        super().__init__()
        self._model = model
        self._max_tokens = max_tokens
        self._client = anthropic.AsyncAnthropic()

    async def complete(self, prompt: str, *, system: str = "", json_mode: bool = False,
                       cls: Class = Class.BATCH, pin_model: bool = False,
                       model: str | None = None) -> Completion:
        use_model = model or self._model
        kwargs: dict = {"model": use_model, "max_tokens": self._max_tokens,
                        "messages": [{"role": "user", "content": prompt}]}
        if system:
            kwargs["system"] = system
        message = await self._client.messages.create(**kwargs)
        text = "".join(block.text for block in message.content if block.type == "text")
        usage = message.usage
        return Completion(text=text, served_provider="anthropic", served_model=use_model,
            input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0)

    async def embed(self, texts: list[str], *, cls: Class = Class.BATCH) -> list[list[float]]:
        raise NotImplementedError("Anthropic does not provide embeddings; configure Ollama embeddings")

    async def aclose(self) -> None:
        await self._client.close()
