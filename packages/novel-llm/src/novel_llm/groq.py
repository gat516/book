"""Groq direct provider through its OpenAI-compatible chat API (§5.4)."""

from __future__ import annotations

import json
import os
from copy import deepcopy

import httpx

from novel_llm.provider import (
    Class, Completion, SequentialBatchMixin, system_with_schema, transient_as_backpressure,
)


STRICT_SCHEMA_MODELS = frozenset({
    "openai/gpt-oss-20b", "openai/gpt-oss-120b", "qwen/qwen3.8-27b",
})


def strict_schema(schema: dict) -> dict:
    """Adapt a copy of the wire schema; application validation remains authoritative."""
    result = deepcopy(schema)

    def visit(node):
        if not isinstance(node, dict):
            return
        node.pop("default", None)
        if node.get("type") == "object" or "properties" in node:
            # Open-ended maps cannot be represented by strict closed objects.
            if node.get("additionalProperties") not in (None, False):
                raise ValueError("Groq strict schema requires closed object properties")
            node["additionalProperties"] = False
            node["required"] = list(node.get("properties", {}))
        for key in ("properties", "$defs", "definitions"):
            for child in node.get(key, {}).values():
                visit(child)
        for key in ("items", "additionalProperties"):
            visit(node.get(key))
        for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
            for child in node.get(key, []):
                visit(child)

    visit(result)
    return result


class GroqRequestError(httpx.HTTPStatusError):
    """Content-free diagnostics: never retain provider prose in exception strings."""

    def __init__(self, response: httpx.Response) -> None:
        try:
            error = response.json().get("error", {})
            code = error.get("code") if isinstance(error, dict) else None
        except (ValueError, AttributeError):
            code = None
        self.provider_code = code if isinstance(code, str) and code in {
            "json_validate_failed", "tool_use_failed", "context_length_exceeded",
            "model_not_found", "model_decommissioned", "invalid_api_key",
            "invalid_request_error", "invalid_value", "unsupported_parameter",
        } else "unclassified"
        super().__init__(f"Groq HTTP {response.status_code}; code={self.provider_code}",
                         request=response.request, response=response)


class GroqProvider(SequentialBatchMixin):
    def __init__(self, *, model: str, base_url: str = "https://api.groq.com/openai/v1",
                 api_key: str | None = None, timeout: float = 120.0) -> None:
        super().__init__()
        self._model = model
        key = api_key or os.environ.get("GROQ_API_KEY")
        if not key:
            raise RuntimeError("GroqProvider needs an api_key or GROQ_API_KEY")
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout,
                                         headers={"Authorization": f"Bearer {key}"})

    async def complete(self, prompt: str, *, system: str = "", json_mode: bool = False,
                       cls: Class = Class.BATCH, pin_model: bool = False,
                       model: str | None = None, json_schema: dict | None = None) -> Completion:
        use_model = model or self._model
        strict = json_schema is not None and use_model in STRICT_SCHEMA_MODELS
        system = system_with_schema(system, None if strict else json_schema)
        if json_mode or json_schema is not None:
            system += "\nReturn only a JSON object matching the requested structure."
        if strict:
            system += (" Include every required top-level field: "
                       + json.dumps(list(json_schema.get("properties", {})))
                       + ". Complete all fields before ending the response; use empty arrays when there are no supported items.")
        messages = ([{"role": "system", "content": system}] if system else [])
        messages.append({"role": "user", "content": prompt})
        payload: dict = {"model": use_model, "messages": messages,
                         "max_completion_tokens": 8192}
        # Reasoning and final JSON share this allowance. An implicit small provider
        # default can leave no budget for the JSON and return json_validate_failed.
        if use_model in {"openai/gpt-oss-20b", "openai/gpt-oss-120b"}:
            payload["include_reasoning"] = False
            payload["reasoning_effort"] = "low"
        if strict:
            # §5.4: provider wire adaptation stays behind the provider protocol.
            # JSON-object mode can fail with HTTP 400 json_validate_failed.
            payload["response_format"] = {"type": "json_schema", "json_schema": {
                "name": "completion", "strict": True, "schema": strict_schema(json_schema),
            }}
        elif json_mode or json_schema is not None:
            payload["response_format"] = {"type": "json_object"}
        async with transient_as_backpressure():
            response = await self._client.post("/chat/completions", json=payload)
            if response.is_error:
                raise GroqRequestError(response)
        body = response.json()
        if body["choices"][0].get("finish_reason") == "length":
            raise RuntimeError("provider output limit reached before a complete response")
        usage = body.get("usage", {}) or {}
        return Completion(
            text=body["choices"][0]["message"]["content"],
            served_provider="groq", served_model=body.get("model", use_model),
            input_tokens=usage.get("prompt_tokens", 0) or 0,
            output_tokens=usage.get("completion_tokens", 0) or 0,
            cache_read_tokens=(usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0,
        )

    async def embed(self, texts: list[str], *, cls: Class = Class.BATCH) -> list[list[float]]:
        raise NotImplementedError("Groq embeddings unused; configure Ollama embeddings")

    async def aclose(self) -> None:
        await self._client.aclose()
