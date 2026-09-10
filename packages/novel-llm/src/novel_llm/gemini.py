"""Gemini hosted provider routed through the shared LiteLLM adapter."""

from __future__ import annotations

import os

from novel_llm.hosted import HostedProvider
from novel_llm.provider import Class


DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"


class GeminiProvider(HostedProvider):
    def __init__(self, *, model: str, base_url: str = DEFAULT_BASE_URL,
                 api_key: str | None = None, timeout: float = 120.0,
                 max_output_tokens: int = 8192) -> None:
        if not (api_key or os.environ.get("GEMINI_API_KEY")):
            raise RuntimeError("GeminiProvider needs an api_key or GEMINI_API_KEY")
        super().__init__(model=model, provider_name="gemini", model_prefix="gemini",
                         api_key=api_key, api_key_env="GEMINI_API_KEY", base_url=base_url,
                         timeout=timeout, max_output_tokens=max_output_tokens)

    async def embed(self, texts: list[str], *, cls: Class = Class.BATCH) -> list[list[float]]:
        raise NotImplementedError("Gemini embeddings unused; configure Ollama embeddings")
