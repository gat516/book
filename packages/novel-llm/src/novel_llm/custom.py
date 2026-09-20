"""User-supplied OpenAI-compatible completion endpoint (§5.4 provider seam)."""

from __future__ import annotations

from dataclasses import replace
import httpx
from novel_llm.accounts import hosted
from novel_llm.netguard import approved_endpoint, PublicTransport

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
        if hosted():
            return await self._private_complete(prompt,system,json_mode,pin_model,model,json_schema,max_output_tokens)
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

    async def _private_complete(self, prompt, system, json_mode, pin_model, model, json_schema, max_output_tokens):
        endpoint = approved_endpoint(self._base_url)
        use_model = model or self._model
        messages, response_format = self._request_material(prompt, system=system, json_mode=json_mode, json_schema=json_schema)
        payload = {"model":use_model,"messages":messages,"max_tokens":max_output_tokens or self._max_output_tokens}
        if response_format is not None: payload["response_format"] = response_format
        try:
            async with httpx.AsyncClient(transport=PublicTransport(), timeout=self._timeout, follow_redirects=False, trust_env=False) as client:
                response = await client.post(endpoint+'/chat/completions', headers={"Authorization":"Bearer "+self._api_key}, json=payload)
                response.raise_for_status()
                result = response.json()
        except Exception as exc:
            self._raise_normalized(exc,schema=json_schema is not None)
            raise
        return replace(self._completion(result, requested_model=use_model, pin_model=pin_model),served_provider="custom")
