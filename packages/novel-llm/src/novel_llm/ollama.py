"""Ollama direct provider."""

from __future__ import annotations

import httpx

from novel_llm.provider import Class, Completion, SequentialBatchMixin


class OllamaProvider(SequentialBatchMixin):
    def __init__(self, *, host: str, model: str, timeout: float = 120.0) -> None:
        super().__init__()
        self._host = host.rstrip("/")
        self._model = model
        self._client = httpx.AsyncClient(base_url=self._host, timeout=timeout)

    async def complete(self, prompt: str, *, system: str = "", json_mode: bool = False,
                       cls: Class = Class.BATCH, pin_model: bool = False,
                       model: str | None = None) -> Completion:
        use_model = model or self._model
        messages = ([{"role": "system", "content": system}] if system else [])
        messages.append({"role": "user", "content": prompt})
        payload: dict = {"model": use_model, "messages": messages, "stream": False}
        if json_mode:
            payload["format"] = "json"
        resp = await self._client.post("/api/chat", json=payload)
        resp.raise_for_status()
        body = resp.json()
        return Completion(text=body["message"]["content"], served_provider="ollama",
                          served_model=use_model, input_tokens=body.get("prompt_eval_count", 0),
                          output_tokens=body.get("eval_count", 0))

    async def embed(self, texts: list[str], *, cls: Class = Class.BATCH) -> list[list[float]]:
        resp = await self._client.post("/api/embed", json={"model": self._model, "input": texts})
        resp.raise_for_status()
        return resp.json()["embeddings"]

    async def aclose(self) -> None:
        await self._client.aclose()
