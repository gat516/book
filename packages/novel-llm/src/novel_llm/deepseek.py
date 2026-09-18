"""DeepSeek hosted provider, routed through the shared LiteLLM adapter."""

from __future__ import annotations

import os
import re
from dataclasses import replace
from typing import Any

from novel_llm.provider import Class, Completion, PinnedModelChanged
from novel_llm.hosted import HostedProvider


# DeepSeek answers a versioned request under the unversioned family name:
# "deepseek-v4-flash" comes back as "deepseek-flash". Only that exact rewrite is accepted
# as the same model; any other served name still fails the pin and the cache guard.
_VERSION_SEGMENT = re.compile(r"-v\d+(?=-)")


def _is_served_alias(requested_model: str, served_model: str) -> bool:
    unversioned = _VERSION_SEGMENT.sub("", requested_model, count=1)
    return unversioned != requested_model and served_model == unversioned


class DeepSeekProvider(HostedProvider):
    def __init__(
        self,
        *,
        model: str,
        base_url: str = "https://api.deepseek.com",
        api_key: str | None = None,
    ) -> None:
        if not (api_key or os.environ.get("DEEPSEEK_API_KEY")):
            raise RuntimeError("DeepSeekProvider needs an api_key or DEEPSEEK_API_KEY")
        super().__init__(model=model, provider_name="deepseek", model_prefix="deepseek",
                         api_key=api_key, api_key_env="DEEPSEEK_API_KEY",
                         base_url=base_url, timeout=120.0)

    async def complete(self, prompt: str, *, reasoning_effort: str | None = None,
                       **kwargs: Any) -> Completion:
        # LiteLLM maps reasoning_effort to DeepSeek's on/off `thinking` switch: "none"
        # turns thinking off, and every other level becomes plain "enabled", silently
        # running at full effort. DeepSeek accepts reasoning_effort itself, so a real
        # level goes in the body where LiteLLM passes it through untouched.
        if reasoning_effort in (None, "none"):
            return await super().complete(prompt, reasoning_effort=reasoning_effort, **kwargs)
        return await self._complete_hosted(prompt, native_json_schema=self._native_json_schema,
                                           extra_body={"reasoning_effort": reasoning_effort}, **kwargs)

    def _completion(self, response: Any, *, requested_model: str,
                    pin_model: bool = False) -> Completion:
        # Map the alias back to the requested name here, at the provider seam, so the
        # pin check, the cache's requested-vs-served guard (§12, §14.3) and translate's
        # provenance all see one identity. The API gives nothing finer than the family
        # name, so this loses no information it would otherwise have recorded.
        completion = super()._completion(response, requested_model=requested_model)
        served = completion.served_model.removeprefix(self.model_prefix + "/")
        if _is_served_alias(requested_model, served):
            completion = replace(completion, served_model=requested_model)
        if pin_model and self._pin_requested_model(requested_model, completion):
            raise PinnedModelChanged("provider changed a pinned model")
        return completion

    async def embed(self, texts: list[str], *, cls: Class = Class.BATCH) -> list[list[float]]:
        raise NotImplementedError("DeepSeek does not provide embeddings; configure Ollama embeddings")
