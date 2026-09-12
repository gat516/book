"""OpenRouter provider, including its OpenAI-shaped embeddings endpoint."""

from __future__ import annotations

import os

from novel_llm.hosted import HostedProvider
from novel_llm.provider import Class


DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterProvider(HostedProvider):
    """LiteLLM chat plus native ``POST /api/v1/embeddings`` support.

    OpenRouter's embeddings router accepts a list of inputs and returns indexed
    ``data`` items. ``embed_dim`` is sent as ``dimensions`` when supplied so the
    response matches the pgvector schema rather than relying on a model default.
    """

    def __init__(self, *, model: str, base_url: str = DEFAULT_BASE_URL,
                 api_key: str | None = None, timeout: float = 120.0,
                 max_output_tokens: int = 8192, embed_model: str | None = None,
                 embed_dim: int | None = None) -> None:
        if not (api_key or os.environ.get("OPENROUTER_API_KEY")):
            raise RuntimeError("OpenRouterProvider needs an api_key or OPENROUTER_API_KEY")
        super().__init__(model=model, provider_name="openrouter", model_prefix="openrouter",
                         api_key=api_key, api_key_env="OPENROUTER_API_KEY", base_url=base_url,
                         timeout=timeout, max_output_tokens=max_output_tokens)
        self._embed_model = embed_model or model
        self._embed_dim = embed_dim

    async def embed(self, texts: list[str], *, cls: Class = Class.BATCH) -> list[list[float]]:
        del cls  # OpenRouter has no priority field on its embeddings endpoint.
        if not texts:
            return []
        payload: dict = {"model": self._embed_model, "input": texts}
        if self._embed_dim is not None:
            payload["dimensions"] = self._embed_dim
        body = await self._embedding_request(
            self._base_url.rstrip("/") + "/embeddings", payload,
            headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
        )
        rows = body.get("data")
        if not isinstance(rows, list) or len(rows) != len(texts):
            raise RuntimeError("OpenRouter returned an unexpected embedding count")
        try:
            ordered = sorted(rows, key=lambda row: int(row["index"]))
            vectors = [row["embedding"] for row in ordered]
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("OpenRouter returned malformed embeddings") from exc
        if any(not isinstance(vector, list) for vector in vectors):
            raise RuntimeError("OpenRouter returned malformed embeddings")
        return vectors
