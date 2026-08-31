"""Gemini direct provider, through Google's OpenAI-compatible endpoint.

Shaped like DeepSeekProvider rather than the google-genai SDK: the compatibility layer
speaks the same /chat/completions dialect, so httpx keeps this dependency-free and keeps
one less vendor SDK out of the pipeline (§5.4 — provider SDKs never reach stage code).

Deliberately NOT done by pointing DeepSeekProvider at Gemini's base_url, which would
otherwise work: every Completion it returns would be stamped served_provider="deepseek",
and that string is durable on the record of what actually served a chapter.
"""

from __future__ import annotations

import os

import httpx

from novel_llm.provider import (
    Class, Completion, SequentialBatchMixin, system_with_schema, transient_as_backpressure,
)

# Google's OpenAI-compat base. The /chat/completions suffix is appended per request, the
# same way DeepSeekProvider treats its own base.
DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"


class GeminiProvider(SequentialBatchMixin):
    def __init__(
        self,
        *,
        model: str,
        base_url: str = DEFAULT_BASE_URL,
        api_key: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        super().__init__()
        self._model = model
        key = api_key or os.environ.get("GEMINI_API_KEY")
        if not key:
            raise RuntimeError("GeminiProvider needs an api_key or GEMINI_API_KEY")
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            headers={"Authorization": f"Bearer {key}"},
        )

    async def complete(self, prompt: str, *, system: str = "", json_mode: bool = False,
                       cls: Class = Class.BATCH, pin_model: bool = False,
                       model: str | None = None, json_schema: dict | None = None) -> Completion:
        system = system_with_schema(system, json_schema)
        use_model = model or self._model
        messages = ([{"role": "system", "content": system}] if system else [])
        messages.append({"role": "user", "content": prompt})
        payload: dict = {"model": use_model, "messages": messages}
        if json_mode or json_schema is not None:
            payload["response_format"] = {"type": "json_object"}
        # A free-tier key makes 429 an ordinary outcome, not an exception -- surface it as
        # backpressure so the worker requeues the chapter instead of failing it.
        async with transient_as_backpressure():
            resp = await self._client.post("/chat/completions", json=payload)
            resp.raise_for_status()
        body = resp.json()
        usage = body.get("usage", {}) or {}
        prompt_tokens = usage.get("prompt_tokens", 0) or 0
        completion_tokens = usage.get("completion_tokens", 0) or 0
        total_tokens = usage.get("total_tokens", 0) or 0
        # Gemini bills thinking tokens as output but leaves them OUT of completion_tokens:
        # an observed reply reported prompt=49, completion=66, total=507. Taking
        # completion_tokens at face value would under-report generated cost by ~4x, so
        # prefer whatever the totals imply when that is larger.
        output_tokens = max(completion_tokens, total_tokens - prompt_tokens)
        return Completion(
            text=body["choices"][0]["message"]["content"],
            served_provider="gemini",
            served_model=body.get("model", use_model),
            input_tokens=prompt_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=(usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0,
        )

    async def embed(self, texts: list[str], *, cls: Class = Class.BATCH) -> list[list[float]]:
        # Same stance as Anthropic/DeepSeek: §5.4 routes embeddings to Ollama regardless of
        # which backend serves completions, so the chunk/entity vectors stay in one space.
        raise NotImplementedError("Gemini embeddings unused; configure Ollama embeddings")

    async def aclose(self) -> None:
        await self._client.aclose()
