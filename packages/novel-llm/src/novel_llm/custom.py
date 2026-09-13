"""User-supplied OpenAI-compatible completion endpoint (§5.4 provider seam)."""

from __future__ import annotations

from dataclasses import replace

from novel_llm.hosted import HostedProvider
from novel_llm.provider import Class, Completion


class CustomProvider(HostedProvider):
    """A named provider for an arbitrary OpenAI-compatible ``/chat/completions`` API.

    LiteLLM routes the wire request through its OpenAI adapter, but durable provenance
    remains ``custom``. That distinction keeps cache keys tied to the configured provider
    rather than pretending every compatible server is OpenAI itself (§5.4, §14.3).
    """

    def __init__(self, *, model: str, base_url: str, api_key: str | None = None,
                 timeout: float = 120.0, max_output_tokens: int = 8192) -> None:
        if not base_url.strip():
            raise RuntimeError("CustomProvider needs a base_url")
        if not api_key:
            raise RuntimeError("CustomProvider needs an api_key")
        super().__init__(
            model=model,
            provider_name="custom",
            model_prefix="openai",
            api_key=api_key,
            api_key_env="CUSTOM_API_KEY",
            base_url=base_url,
            timeout=timeout,
            max_output_tokens=max_output_tokens,
        )

    def _pin_requested_model(self, requested_model: str, completion: Completion) -> bool:
        served_model = completion.served_model.removeprefix("openai/")
        return served_model != requested_model

    async def complete(self, prompt: str, *, system: str = "", json_mode: bool = False,
                       cls: Class = Class.BATCH, pin_model: bool = False,
                       model: str | None = None, json_schema: dict | None = None,
                       max_output_tokens: int | None = None) -> Completion:
        completion = await super().complete(
            prompt,
            system=system,
            json_mode=json_mode,
            cls=cls,
            pin_model=pin_model,
            model=model,
            json_schema=json_schema,
            max_output_tokens=max_output_tokens,
        )
        return replace(completion, served_provider="custom")
