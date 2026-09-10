"""Groq direct provider through its OpenAI-compatible chat API (§5.4)."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from typing import Literal

from novel_llm.provider import (
    Class, Completion, system_with_schema,
)
from novel_llm.hosted import HostedProvider


STRICT_SCHEMA_MODELS = frozenset({
    "openai/gpt-oss-20b", "openai/gpt-oss-120b",
})


def _collapse_ambiguous_union(node: dict, widened: list[list] | None) -> None:
    """Merge union branches that share one primitive type, in place.

    Groq compiles the schema into a decoding constraint, so every branch must be
    distinguishable at the moment a value starts. Two branches that are both `string`
    are not: it rejects the whole request with `duplicate_primitive_types` before the
    model runs. The contract legitimately produces one -- a vocabulary term is
    "a known term (enum) OR a new snake_case term (pattern)" (passages.py) -- so the
    adaptation belongs here, at the provider seam, rather than in the shared contract
    that other providers accept as-is (§5.4).

    Merging never narrows: branches sharing a type collapse to the union of what they
    accepted, which for enum-plus-pattern is the pattern branch alone. Branches of
    DIFFERENT types (the nullable `{string} | {null}` references) are unambiguous and
    are left exactly as they are.

    Any enum dropped this way is reported through ``widened`` so the caller can keep
    its steering value by naming the terms in the prompt; a strict request never
    carries the schema itself.
    """
    for key in ("anyOf", "oneOf"):
        branches = node.get(key)
        if not isinstance(branches, list) or len(branches) < 2:
            continue
        groups: dict = {}
        order: list = []
        for index, branch in enumerate(branches):
            kind = branch.get("type") if isinstance(branch, dict) else None
            # Only a named primitive type can collide. Anything else keeps its own slot.
            marker = kind if isinstance(kind, str) else f"#{index}"
            if marker not in groups:
                groups[marker] = []
                order.append(marker)
            groups[marker].append(branch)
        if all(len(groups[m]) == 1 for m in order):
            continue
        merged = []
        for marker in order:
            group = groups[marker]
            if len(group) == 1:
                merged.append(group[0])
                continue
            if all(isinstance(b, dict) and "enum" in b for b in group):
                values: list = []
                for branch in group:
                    for value in branch["enum"]:
                        if value not in values:
                            values.append(value)
                merged.append({"type": group[0]["type"], "enum": values})
                continue
            # One branch was open (a pattern, or unconstrained), so it already admits
            # everything the enum branches did. Keep the type and drop the constraints.
            if widened is not None:
                for branch in group:
                    if isinstance(branch, dict) and branch.get("enum"):
                        widened.append(list(branch["enum"]))
            merged.append({"type": group[0]["type"]})
        if len(merged) == 1:
            node.pop(key)
            node.update(merged[0])
        else:
            node[key] = merged


def strict_schema(schema: dict, widened: list[list] | None = None) -> dict:
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
        # After the children are normalized, so a merged branch is already adapted.
        _collapse_ambiguous_union(node, widened)

    visit(result)
    return result


# A vocabulary can grow without bound, while the steering sentence it feeds cannot: the
# prompt is charged per token on every call. Cap it and let the schema-free reminder do
# the rest -- the application still canonicalizes and validates whatever comes back.
PREFERRED_TERM_LIMIT = 120


def _preferred_terms(widened: list[list]) -> str:
    values: list[str] = []
    for group in widened:
        for value in group:
            if isinstance(value, str) and value not in values:
                values.append(value)
    if not values:
        return ""
    shown = values[:PREFERRED_TERM_LIMIT]
    text = ", ".join(shown)
    return text + (", ..." if len(values) > len(shown) else "")


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
        # Adapt first: collapsing an ambiguous union can drop an enum whose only other
        # route to the model is this prompt, since a strict request omits the schema.
        widened: list[list] = []
        wire_schema = strict_schema(json_schema, widened) if strict else None
        system = system_with_schema(system, None if strict else json_schema)
        if json_mode or json_schema is not None:
            system += "\nReturn only a JSON object matching the requested structure."
        if strict:
            system += (" Include every required top-level field: "
                       + json.dumps(list(json_schema.get("properties", {})))
                       + ". Complete all fields before ending the response; use empty arrays when there are no supported items.")
            preferred = _preferred_terms(widened)
            if preferred:
                # Steering only. The schema can no longer restrict these to a list, so
                # say so in words: reusing an established term is what keeps a book's
                # vocabulary from fragmenting into synonyms nobody merged.
                system += (" Reuse one of these existing terms whenever one fits, and"
                           " only invent a new lower_snake_case term when none does: "
                           + preferred + ".")
        # Keep the Groq model-specific strict adaptation local to this provider. The
        # capability is passed as a local argument so concurrent calls cannot race.
        return await self._complete_hosted(prompt, system=system, json_mode=json_mode,
                                           cls=cls, pin_model=pin_model, model=model,
                                           json_schema=wire_schema,
                                           max_output_tokens=max_output_tokens,
                                           native_json_schema=True)

    async def embed(self, texts: list[str], *, cls: Class = Class.BATCH) -> list[list[float]]:
        raise NotImplementedError("Groq embeddings unused; configure Ollama embeddings")
