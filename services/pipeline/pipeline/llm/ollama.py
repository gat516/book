"""Ollama backend — the local dev workhorse (free, so prompt iteration costs nothing).

Talks to a local Ollama server over HTTP: ``/api/chat`` for completions,
``/api/embeddings`` for vectors (``nomic-embed-text``). Inherits the sequential batch
default from ``SequentialBatchMixin`` — Ollama has no native Batch API.
"""

from __future__ import annotations

import httpx

from pipeline.llm.provider import SequentialBatchMixin


class OllamaProvider(SequentialBatchMixin):
    def __init__(self, *, host: str, model: str, timeout: float = 120.0) -> None:
        super().__init__()
        self._host = host.rstrip("/")
        self._model = model
        self._client = httpx.AsyncClient(base_url=self._host, timeout=timeout)

    async def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        json_mode: bool = False,
    ) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload: dict = {"model": self._model, "messages": messages, "stream": False}
        if json_mode:
            payload["format"] = "json"

        resp = await self._client.post("/api/chat", json=payload)
        resp.raise_for_status()
        return resp.json()["message"]["content"]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        # /api/embeddings takes one prompt at a time; call per text.
        vectors: list[list[float]] = []
        for text in texts:
            resp = await self._client.post(
                "/api/embeddings", json={"model": self._model, "prompt": text}
            )
            resp.raise_for_status()
            vectors.append(resp.json()["embedding"])
        return vectors

    async def aclose(self) -> None:
        await self._client.aclose()
