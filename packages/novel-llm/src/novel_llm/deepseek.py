"""DeepSeek hosted provider, routed through the shared LiteLLM adapter."""

from __future__ import annotations

import os

from novel_llm.provider import Class
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

    async def embed(self, texts: list[str], *, cls: Class = Class.BATCH) -> list[list[float]]:
        raise NotImplementedError("DeepSeek does not provide embeddings; configure Ollama embeddings")
