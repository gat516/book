"""Anthropic direct provider."""

from __future__ import annotations

from novel_llm.provider import Class
from novel_llm.hosted import HostedProvider


class AnthropicProvider(HostedProvider):
    def __init__(self, *, model: str, max_tokens: int = 8192, api_key: str | None = None,
                 base_url: str | None = None, max_output_tokens: int | None = None) -> None:
        super().__init__(model=model, provider_name="anthropic", model_prefix="anthropic",
                         api_key=api_key, api_key_env="ANTHROPIC_API_KEY",
                         base_url=base_url, max_output_tokens=max_output_tokens or max_tokens)
