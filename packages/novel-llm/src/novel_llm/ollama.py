"""Ollama direct provider."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

import httpx

from novel_llm.provider import Class, Completion, SequentialBatchMixin

# Called with the text accumulated SO FAR (not the latest token), so a consumer can simply
# overwrite whatever it stored last rather than reassembling a token stream.
StreamSink = Callable[[str], Awaitable[None]]


class OllamaProvider(SequentialBatchMixin):
    def __init__(self, *, host: str, model: str, timeout: float = 120.0) -> None:
        super().__init__()
        self._host = host.rstrip("/")
        self._model = model
        self._client = httpx.AsyncClient(base_url=self._host, timeout=timeout)
        # Optional observer for partial output. Set it to watch a long completion arrive
        # incrementally; leave it None and nothing about this provider changes.
        #
        # An attribute rather than a complete() parameter on purpose: completions reach
        # this provider through the shared LLMProvider protocol and the batch boundary,
        # and threading a per-call callback through both would change signatures every
        # other backend implements, for something only this one can do. The caller scopes
        # it instead — the worker sets it around the one stage whose output is prose and
        # clears it afterwards, so JSON-emitting stages never feed it.
        self.stream_sink: StreamSink | None = None

    async def complete(self, prompt: str, *, system: str = "", json_mode: bool = False,
                       cls: Class = Class.BATCH, pin_model: bool = False,
                       model: str | None = None) -> Completion:
        use_model = model or self._model
        messages = ([{"role": "system", "content": system}] if system else [])
        messages.append({"role": "user", "content": prompt})
        sink = self.stream_sink
        payload: dict = {"model": use_model, "messages": messages, "stream": sink is not None}
        if json_mode:
            payload["format"] = "json"
        if sink is None:
            resp = await self._client.post("/api/chat", json=payload)
            resp.raise_for_status()
            body = resp.json()
            return Completion(text=body["message"]["content"], served_provider="ollama",
                              served_model=use_model, input_tokens=body.get("prompt_eval_count", 0),
                              output_tokens=body.get("eval_count", 0))
        return await self._complete_streaming(payload, use_model, sink)

    async def _complete_streaming(self, payload: dict, use_model: str, sink: StreamSink) -> Completion:
        """Same contract as the buffered path — a finished Completion — but reports partial
        text to `sink` while the response arrives. Ollama streams newline-delimited JSON,
        one object per token, with the final object carrying the token counts."""
        pieces: list[str] = []
        input_tokens = output_tokens = 0
        async with self._client.stream("POST", "/api/chat", json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                body = json.loads(line)
                piece = body.get("message", {}).get("content", "")
                if piece:
                    pieces.append(piece)
                    # Sink failures must never fail the completion: this is an observer,
                    # and losing a preview update is not a reason to lose the translation.
                    try:
                        await sink("".join(pieces))
                    except Exception:  # noqa: BLE001
                        pass
                if body.get("done"):
                    input_tokens = body.get("prompt_eval_count", 0)
                    output_tokens = body.get("eval_count", 0)
        return Completion(text="".join(pieces), served_provider="ollama",
                          served_model=use_model, input_tokens=input_tokens,
                          output_tokens=output_tokens)

    async def embed(self, texts: list[str], *, cls: Class = Class.BATCH) -> list[list[float]]:
        resp = await self._client.post("/api/embed", json={"model": self._model, "input": texts})
        resp.raise_for_status()
        return resp.json()["embeddings"]

    async def aclose(self) -> None:
        await self._client.aclose()
