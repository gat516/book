"""Gemini hosted provider routed through the shared LiteLLM adapter."""

from __future__ import annotations

import os

from novel_llm.hosted import HostedProvider
from novel_llm.provider import Class


DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
DEFAULT_EMBED_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


class GeminiProvider(HostedProvider):
    def __init__(self, *, model: str, base_url: str = DEFAULT_BASE_URL,
                 api_key: str | None = None, timeout: float = 120.0,
                 max_output_tokens: int = 8192, embed_model: str | None = None,
                 embed_dim: int | None = None,
                 embed_base_url: str = DEFAULT_EMBED_BASE_URL) -> None:
        if not (api_key or os.environ.get("GEMINI_API_KEY")):
            raise RuntimeError("GeminiProvider needs an api_key or GEMINI_API_KEY")
        super().__init__(model=model, provider_name="gemini", model_prefix="gemini",
                         api_key=api_key, api_key_env="GEMINI_API_KEY", base_url=base_url,
                         timeout=timeout, max_output_tokens=max_output_tokens)
        self._embed_model = (embed_model or model).removeprefix("models/")
        self._embed_dim = embed_dim
        self._embed_base_url = embed_base_url.rstrip("/")

    async def embed(self, texts: list[str], *, cls: Class = Class.BATCH) -> list[list[float]]:
        if not texts:
            return []
        task_type = "RETRIEVAL_DOCUMENT" if cls == Class.BATCH else "RETRIEVAL_QUERY"
        requests = []
        for text in texts:
            request = {
                "model": f"models/{self._embed_model}",
                "content": {"parts": [{"text": text}]},
            }
            # Gemini Embedding 2 uses prompt task instructions instead of the legacy
            # taskType field; embedding-001 accepts the retrieval task optimization.
            if self._embed_model != "gemini-embedding-2":
                request["taskType"] = task_type
            if self._embed_dim is not None:
                request["outputDimensionality"] = self._embed_dim
            requests.append(request)
        body = await self._embedding_request(
            f"{self._embed_base_url}/models/{self._embed_model}:batchEmbedContents",
            {"requests": requests},
            headers={"x-goog-api-key": self._api_key, "Content-Type": "application/json"},
        )
        rows = body.get("embeddings")
        if not isinstance(rows, list) or len(rows) != len(texts):
            raise RuntimeError("Gemini returned an unexpected embedding count")
        try:
            vectors = [row["values"] for row in rows]
        except (KeyError, TypeError) as exc:
            raise RuntimeError("Gemini returned malformed embeddings") from exc
        if any(not isinstance(vector, list) for vector in vectors):
            raise RuntimeError("Gemini returned malformed embeddings")
        return vectors
