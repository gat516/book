"""Ollama direct provider."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable

import httpx

from novel_llm.provider import Class, Completion, SequentialBatchMixin
from novel_llm.admission import ollama_session

# Called with the text accumulated SO FAR (not the latest token), so a consumer can simply
# overwrite whatever it stored last rather than reassembling a token stream.
StreamSink = Callable[[str], Awaitable[None]]


class OllamaProvider(SequentialBatchMixin):
    def __init__(self, *, host: str, model: str, timeout: float = 120.0,
                 num_ctx: int | None = None, num_predict: int | None = None,
                 stream: bool = False, total_timeout: float | None = None,
                 first_token_timeout: float | None = None) -> None:
        super().__init__()
        self._host = host.rstrip("/")
        self._model = model
        self._options = {k:v for k,v in {"num_ctx":num_ctx,"num_predict":num_predict}.items() if v is not None}
        if timeout <= 0 or (total_timeout is not None and total_timeout <= 0):
            raise ValueError("Ollama timeouts must be positive")
        if first_token_timeout is not None and first_token_timeout <= 0:
            raise ValueError("Ollama timeouts must be positive")
        self._stream = stream
        self._total_timeout = total_timeout
        # Prefill and inter-token stalls are different failures with different healthy
        # durations. Ollama sends no bytes at all while processing the prompt, so a single
        # read timeout covering both has to be sized for the slowest prefill — which makes
        # it useless for catching a stream that dies mid-generation. Defaults to `timeout`
        # so callers that never cared keep their existing single-budget behavior.
        self._idle_timeout = timeout
        self._first_token_timeout = timeout if first_token_timeout is None else first_token_timeout
        # httpx no longer arbitrates read deadlines: the budgets below do, per chunk, so
        # they can differ before and after the first token. Connect/pool stay short.
        read_ceiling = total_timeout if total_timeout is not None else max(self._idle_timeout, self._first_token_timeout)
        self._client = httpx.AsyncClient(base_url=self._host,
            timeout=httpx.Timeout(read_ceiling, connect=min(timeout, 10), pool=min(timeout, 10)))
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
                       model: str | None = None, json_schema: dict | None = None) -> Completion:
        async with ollama_session(self._host, timeout=0 if cls == Class.INTERACTIVE else 30) as waited:
            started = time.monotonic()
            deadline = asyncio.timeout(self._total_timeout)
            try:
                async with deadline:
                    result = await self._complete(prompt, system=system, json_mode=json_mode,
                                                  model=model, json_schema=json_schema)
            except TimeoutError as exc:
                # Do not relabel an explicit prefill/idle timeout as the total deadline.
                # asyncio.timeout(None) is deliberately a no-op deadline, but exceptions
                # raised inside its body still reach this handler.
                if not deadline.expired():
                    raise
                raise TimeoutError(f"Ollama {model or self._model} exceeded total inference deadline "
                                   f"of {self._total_timeout}s (admission wait {waited:.2f}s)") from exc
            result.timings.update(admission_wait_seconds=waited, request_seconds=time.monotonic() - started)
            return result

    async def _complete(self, prompt: str, *, system: str, json_mode: bool,
                        model: str | None, json_schema: dict | None) -> Completion:
        use_model = model or self._model
        messages = ([{"role": "system", "content": system}] if system else [])
        messages.append({"role": "user", "content": prompt})
        sink = self.stream_sink
        payload: dict = {"model": use_model, "messages": messages, "stream": self._stream or sink is not None}
        if json_schema is not None:
            payload["format"] = json_schema
            payload["options"] = {"temperature": 0, **self._options}
        elif json_mode:
            payload["format"] = "json"
        if self._options and "options" not in payload:
            payload["options"] = dict(self._options)
        if not payload["stream"]:
            resp = await self._client.post("/api/chat", json=payload)
            resp.raise_for_status()
            body = resp.json()
            self._check_body(body)
            return Completion(text=body["message"]["content"], served_provider="ollama",
                              served_model=body.get("model", use_model), input_tokens=body.get("prompt_eval_count", 0),
                              output_tokens=body.get("eval_count", 0), timings=self._timings(body))
        return await self._complete_streaming(payload, use_model, sink)

    @staticmethod
    def _check_body(body: dict) -> None:
        if body.get("error"):
            raise RuntimeError(f"Ollama stream error: {body['error']}")
        if body.get("done_reason") == "length":
            raise RuntimeError("Ollama exhausted num_predict; refusing incomplete output")

    @staticmethod
    def _timings(body: dict) -> dict[str, float]:
        # Ollama reports durations in nanoseconds, independent of HTTP/admission time.
        return {name.removesuffix('_duration') + '_seconds': body[name] / 1e9
                for name in ('total_duration', 'load_duration', 'prompt_eval_duration', 'eval_duration')
                if isinstance(body.get(name), (int, float))}

    async def _complete_streaming(self, payload: dict, use_model: str, sink: StreamSink | None) -> Completion:
        """Same contract as the buffered path — a finished Completion — but reports partial
        text to `sink` while the response arrives. Ollama streams newline-delimited JSON,
        one object per token, with the final object carrying the token counts."""
        pieces: list[str] = []
        final = None
        started = time.monotonic()
        first_token = None
        async with self._client.stream("POST", "/api/chat", json=payload) as resp:
            resp.raise_for_status()
            lines = resp.aiter_lines().__aiter__()
            while True:
                # Arm the prefill budget until the model actually speaks, the idle budget
                # after. Whichever expires names itself, so a stalled run is diagnosable
                # from the failure alone.
                phase = "generation" if first_token is not None else "prefill"
                budget = self._idle_timeout if first_token is not None else self._first_token_timeout
                try:
                    line = await asyncio.wait_for(anext(lines), budget)
                except StopAsyncIteration:
                    break
                except TimeoutError as exc:
                    raise TimeoutError(
                        f"Ollama {use_model} exceeded its {budget}s {phase} budget after "
                        f"{time.monotonic() - started:.1f}s with {len(pieces)} tokens received"
                    ) from exc
                if not line.strip():
                    continue
                body = json.loads(line)
                self._check_body(body)
                piece = body.get("message", {}).get("content", "")
                if piece:
                    if first_token is None:
                        first_token = time.monotonic() - started
                    pieces.append(piece)
                    # Sink failures must never fail the completion: this is an observer,
                    # and losing a preview update is not a reason to lose the translation.
                    if sink is not None:
                        try:
                            await sink("".join(pieces))
                        except Exception:  # noqa: BLE001
                            pass
                if body.get("done"):
                    final = body
                    break
        if final is None:
            raise RuntimeError("Ollama stream ended without done=true; refusing partial output")
        timings = self._timings(final)
        if first_token is not None:
            timings['first_token_seconds'] = first_token
        return Completion(text="".join(pieces), served_provider="ollama",
                          served_model=final.get("model", use_model), input_tokens=final.get("prompt_eval_count", 0),
                          output_tokens=final.get("eval_count", 0), timings=timings)

    async def embed(self, texts: list[str], *, cls: Class = Class.BATCH) -> list[list[float]]:
        async with ollama_session(self._host, timeout=0 if cls == Class.INTERACTIVE else 30):
            async with asyncio.timeout(self._total_timeout):
                resp = await self._client.post("/api/embed", json={"model": self._model, "input": texts})
                resp.raise_for_status()
                return resp.json()["embeddings"]

    async def aclose(self) -> None:
        await self._client.aclose()
