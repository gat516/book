"""DeepSeek direct provider — an OpenAI-shaped chat-completions API, so this is built
with httpx like OllamaProvider rather than a vendor SDK (DeepSeek has none for Python).
No embeddings endpoint, no batch API — mirrors AnthropicProvider on both counts.
"""

from __future__ import annotations

import os

import httpx

from novel_llm.provider import (
    Class, Completion, SequentialBatchMixin, system_with_schema, transient_as_backpressure,
)


class DeepSeekProvider(SequentialBatchMixin):
    def __init__(
        self,
        *,
        model: str,
        base_url: str = "https://api.deepseek.com",
        api_key: str | None = None,
    ) -> None:
        super().__init__()
        self._model = model
        key = api_key or os.environ.get("DEEPSEEK_API_KEY")
        if not key:
            raise RuntimeError("DeepSeekProvider needs an api_key or DEEPSEEK_API_KEY")
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=120.0,
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
        async with transient_as_backpressure():
            resp = await self._client.post("/chat/completions", json=payload)
            resp.raise_for_status()
        body = resp.json()
        usage = body.get("usage", {})
        return Completion(
            text=body["choices"][0]["message"]["content"],
            served_provider="deepseek",
            served_model=body.get("model", use_model),
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            # DeepSeek's context-caching hits surface as this field on shared prefixes.
            cache_read_tokens=usage.get("prompt_cache_hit_tokens", 0) or 0,
        )

    async def embed(self, texts: list[str], *, cls: Class = Class.BATCH) -> list[list[float]]:
        raise NotImplementedError("DeepSeek does not provide embeddings; configure Ollama embeddings")

    async def aclose(self) -> None:
        await self._client.aclose()
