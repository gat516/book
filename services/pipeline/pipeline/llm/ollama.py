"""Ollama backend — the local dev workhorse (free, so prompt iteration costs nothing).

Talks to a local Ollama server over HTTP: ``/api/chat`` for completions, ``/api/embed``
for vectors (``nomic-embed-text``). Inherits the sequential batch default from
``SequentialBatchMixin`` — Ollama has no native Batch API.

Uses ``/api/embed`` (the current batched endpoint), not the deprecated single-prompt
``/api/embeddings``: it accepts the full ``input`` array in one call and parallelizes
internally, which is both faster for bulk chunk embedding and the shape a future gateway
integration's Embed admission assumes (gateway spec D11 — the unit of admission is the
whole call, not one permit per input).

Ollama has no request-priority mechanism (FIFO to ``OLLAMA_MAX_QUEUE``, then 503) and no
prompt-cache concept, so ``cls`` is accepted but unused here and ``Completion``'s cache
fields are always 0 — see instructions.md §14.4/§14.5 for why that asymmetry with Ollama
is exactly the gap the gateway project exists to fill.
"""

from __future__ import annotations

import httpx

from pipeline.llm.provider import Class, Completion, SequentialBatchMixin


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
        cls: Class = Class.BATCH,
        pin_model: bool = False,
        model: str | None = None,
    ) -> Completion:
        # Ollama has no failover to pin against; pin_model is accepted for interface
        # parity with a future gateway backend and has no effect here.
        use_model = model or self._model
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload: dict = {"model": use_model, "messages": messages, "stream": False}
        if json_mode:
            payload["format"] = "json"

        resp = await self._client.post("/api/chat", json=payload)
        resp.raise_for_status()
        body = resp.json()
        return Completion(
            text=body["message"]["content"],
            served_provider="ollama",
            served_model=use_model,
            input_tokens=body.get("prompt_eval_count", 0),
            output_tokens=body.get("eval_count", 0),
        )

    async def embed(self, texts: list[str], *, cls: Class = Class.BATCH) -> list[list[float]]:
        resp = await self._client.post(
            "/api/embed", json={"model": self._model, "input": texts}
        )
        resp.raise_for_status()
        return resp.json()["embeddings"]

    async def aclose(self) -> None:
        await self._client.aclose()
