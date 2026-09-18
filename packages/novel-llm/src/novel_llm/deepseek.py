"""DeepSeek hosted provider, routed through the shared LiteLLM adapter."""

from __future__ import annotations

import os
from typing import Any

from novel_llm.provider import Class, Completion
from novel_llm.hosted import HostedProvider


class DeepSeekProvider(HostedProvider):
    def __init__(
        self,
        *,
        model: str,
        base_url: str = "https://api.deepseek.com",
        api_key: str | None = None,
    ) -> None:
        if not (api_key or os.environ.get("DEEPSEEK_API_KEY")):
            raise RuntimeError("DeepSeekProvider needs an api_key or DEEPSEEK_API_KEY")
        super().__init__(model=model, provider_name="deepseek", model_prefix="deepseek",
                         api_key=api_key, api_key_env="DEEPSEEK_API_KEY",
                         base_url=base_url, timeout=120.0)

    async def complete(self, prompt: str, *, reasoning_effort: str | None = None,
                       **kwargs: Any) -> Completion:
        # LiteLLM maps reasoning_effort to DeepSeek's on/off `thinking` switch: "none"
        # turns thinking off, and every other level becomes plain "enabled", silently
        # running at full effort. DeepSeek accepts reasoning_effort itself, so a real
        # level goes in the body where LiteLLM passes it through untouched.
        if reasoning_effort in (None, "none"):
            return await super().complete(prompt, reasoning_effort=reasoning_effort, **kwargs)
        return await self._complete_hosted(prompt, native_json_schema=self._native_json_schema,
                                           extra_body={"reasoning_effort": reasoning_effort}, **kwargs)

    async def embed(self, texts: list[str], *, cls: Class = Class.BATCH) -> list[list[float]]:
        raise NotImplementedError("DeepSeek does not provide embeddings; configure Ollama embeddings")
