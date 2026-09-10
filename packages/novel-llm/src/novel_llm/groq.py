"""Groq direct provider through its OpenAI-compatible chat API (§5.4)."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from typing import Literal

from novel_llm.provider import (
    Class, Completion, UnsupportedSchema, system_with_schema,
)
from novel_llm.hosted import HostedProvider


STRICT_SCHEMA_MODELS = frozenset({
    "openai/gpt-oss-20b", "openai/gpt-oss-120b",
})


def strict_schema(schema: dict) -> dict:
    """Normalize only safe Groq strict-schema structure.

    The application validator remains authoritative (§5.4), but this adapter never
    widens a schema to make Groq accept it. Ambiguous unions, open maps, and unsupported
    schema constructs fail before a hosted request can consume budget.
    """
    result = deepcopy(schema)

    def visit(node):
        if not isinstance(node, dict):
            return
        for keyword in ("allOf", "prefixItems", "not", "if", "then", "else",
                        "dependentSchemas", "unevaluatedProperties"):
            if keyword in node:
                raise UnsupportedSchema(f"Groq strict schema does not support {keyword}")
        for key in ("anyOf", "oneOf"):
            if key not in node:
                continue
            branches = node.get(key)
            if not isinstance(branches, list):
                raise UnsupportedSchema(f"Groq strict schema requires {key} branches")
            kinds = [branch.get("type") if isinstance(branch, dict) else None
                     for branch in branches]
            named = [kind for kind in kinds if isinstance(kind, str)]
            if len(named) != len(set(named)):
                raise UnsupportedSchema(
                    f"Groq strict schema cannot represent overlapping {key} primitive types")
        node.pop("default", None)
        if node.get("type") == "object" or "properties" in node:
            # Open-ended maps cannot be represented by strict closed objects.
            if node.get("additionalProperties") not in (None, False):
                raise UnsupportedSchema("Groq strict schema requires closed object properties")
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


class GroqProvider(HostedProvider):
    def __init__(self, *, model: str, base_url: str = "https://api.groq.com/openai/v1",
                 api_key: str | None = None, timeout: float = 120.0,
                 max_output_tokens: int = 8192) -> None:
        if not (api_key or os.environ.get("GROQ_API_KEY")):
            raise RuntimeError("GroqProvider needs an api_key or GROQ_API_KEY")
        super().__init__(model=model, provider_name="groq", model_prefix="groq",
                         api_key=api_key, api_key_env="GROQ_API_KEY", base_url=base_url,
                         timeout=timeout, max_output_tokens=max_output_tokens,
                         native_json_schema=model in STRICT_SCHEMA_MODELS)

    def schema_transport(self, model: str | None = None) -> Literal["native", "prompt"]:
        effective_model = model or self._model
        if effective_model.startswith("groq/"):
            effective_model = effective_model.removeprefix("groq/")
        return "native" if effective_model in STRICT_SCHEMA_MODELS else "prompt"

    async def complete(self, prompt: str, *, system: str = "", json_mode: bool = False,
                       cls: Class = Class.BATCH, pin_model: bool = False,
                       model: str | None = None, json_schema: dict | None = None,
                       max_output_tokens: int | None = None) -> Completion:
        use_model = model or self._model
        strict = json_schema is not None and self.schema_transport(use_model) == "native"
        if not strict:
            return await self._complete_hosted(
                prompt, system=system, json_mode=json_mode, cls=cls,
                pin_model=pin_model, model=model, json_schema=json_schema,
                max_output_tokens=max_output_tokens, native_json_schema=False)
        # Validate and normalize the strict wire schema before transport. Unsupported
        # constructs are rejected so strict decoding never silently weakens extraction.
        wire_schema = strict_schema(json_schema)
        system = system_with_schema(system, None if strict else json_schema)
        if json_mode or json_schema is not None:
            system += "\nReturn only a JSON object matching the requested structure."
        if strict:
            system += (" Include every required top-level field: "
                       + json.dumps(list(json_schema.get("properties", {})))
                       + ". Complete all fields before ending the response; use empty arrays when there are no supported items.")
        # Keep the Groq model-specific strict adaptation local to this provider. The
        # capability is passed as a local argument so concurrent calls cannot race.
        return await self._complete_hosted(prompt, system=system, json_mode=json_mode,
                                           cls=cls, pin_model=pin_model, model=model,
                                           json_schema=wire_schema,
                                           max_output_tokens=max_output_tokens,
                                           native_json_schema=True)

    async def embed(self, texts: list[str], *, cls: Class = Class.BATCH) -> list[list[float]]:
        raise NotImplementedError("Groq embeddings unused; configure Ollama embeddings")
