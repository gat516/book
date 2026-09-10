"""Shared LiteLLM backed adapter for hosted completion providers.

Only this module knows the LiteLLM SDK.  The rest of the engine talks in terms of the
small :class:`LLMProvider` contract, which keeps provider routing and SDK failures out of
pipeline stages (§5.4).  LiteLLM's retry and fallback features are deliberately disabled:
the worker owns retry policy and a fallback would change durable served-model provenance
and translation voice (§14.3).
"""

from __future__ import annotations

import asyncio
import inspect
import os
import re
from collections.abc import Mapping
from typing import Any, Literal

from novel_llm.provider import (
    AdmissionRejected,
    Class,
    Completion,
    RequestBudgetExceeded,
    SequentialBatchMixin,
    TruncatedOutput,
    UnsupportedSchema,
    PinnedModelChanged,
    system_with_schema,
)

_RETRY_HINT = re.compile(r'(?:retry in|"?retryDelay"?\s*:\s*")\s*([0-9]+(?:\.[0-9]+)?)\s*s', re.I)
_SERVER_RETRY_S = 5.0
_RETRYABLE_SERVER_STATUS = frozenset({408, 500, 502, 503, 504})
_NETWORK_ERROR_NAMES = frozenset({
    "apiconnectionerror", "connectionerror", "connecterror", "connecttimeout",
    "readtimeout", "writetimeout", "timeout", "timeouterror", "networkerror",
    "serviceunavailableerror", "unavailableerror",
})
_SCHEMA_ERROR_CODES = frozenset({
    "invalid_response_format", "response_format_not_supported", "unsupported_schema",
    "json_schema_not_supported", "schema_not_supported", "unsupported_parameter",
})

# LiteLLM otherwise refreshes its model-cost map at import time. Provider startup must
# remain offline and deterministic; the adapter only needs the SDK transport here.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
try:  # Keep import-time errors useful in environments installing dependencies lazily.
    import litellm  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - exercised by minimal source checkouts
    litellm = None  # type: ignore[assignment]


_SAFE_RATE_HEADERS = frozenset({
    "retry-after", "ratelimit-reset", "x-ratelimit-reset",
    "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens",
    "x-ratelimit-limit-requests", "x-ratelimit-remaining-requests",
    "x-ratelimit-limit-tokens", "x-ratelimit-remaining-tokens",
})


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _headers(value: Any) -> dict[str, str]:
    headers = _field(value, "headers", {})
    if not isinstance(headers, Mapping):
        return {}
    return {str(k).lower(): str(v) for k, v in headers.items()
            if str(k).lower() in _SAFE_RATE_HEADERS}


def _response_from_error(exc: BaseException) -> Any:
    response = getattr(exc, "response", None)
    if response is not None:
        return response
    return getattr(exc, "http_response", None)


def _status_from_error(exc: BaseException) -> int | None:
    response = _response_from_error(exc)
    status = getattr(response, "status_code", None) if response is not None else None
    if status is None:
        status = getattr(exc, "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _retry_after(headers: Mapping[str, str]) -> float | None:
    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


def _body_retry_hint(exc: BaseException) -> float | None:
    response = _response_from_error(exc)
    body = str(getattr(response, "text", "") or "") if response is not None else ""
    match = _RETRY_HINT.search(body)
    return float(match.group(1)) if match else None


def _quota_category(exc: BaseException, status: int) -> str:
    response = _response_from_error(exc)
    body = str(getattr(response, "text", "") or "").lower() if response is not None else ""
    headers = _headers(response)
    delay = _retry_after(headers)
    if delay is None:
        delay = _body_retry_hint(exc)
    if delay is not None and delay > 3600:
        return "quota_exhausted"
    if any(marker in body for marker in ("per-day", "per day", "daily", "24 hours", "day quota")):
        return "quota_exhausted"
    return "rate_limited" if status == 429 else "model_server_error"


def _structured_error(exc: BaseException) -> tuple[str, str]:
    """Return bounded code/message fields used only for classification."""
    response = _response_from_error(exc)
    payload = getattr(exc, "body", None)
    if payload is None and response is not None:
        try:
            payload = response.json()
        except Exception:  # noqa: BLE001
            payload = None
    if not isinstance(payload, Mapping):
        payload = {}
    error = payload.get("error", payload)
    if not isinstance(error, Mapping):
        error = {}
    code = str(error.get("code") or error.get("type") or "").lower()
    message = str(error.get("message") or getattr(exc, "message", "") or "").lower()
    return code, message


def _retry_delay(exc: BaseException, *, fallback: float) -> tuple[float, bool, dict[str, str]]:
    response = _response_from_error(exc)
    safe = _headers(response)
    delay = _retry_after(safe)
    if delay is None:
        delay = _body_retry_hint(exc)
    return delay if delay is not None else fallback, delay is not None, safe


class HostedProvider(SequentialBatchMixin):
    """LiteLLM completion adapter shared by Groq, DeepSeek, and Anthropic."""

    provider_name: str
    model_prefix: str
    api_key_env: str

    def __init__(self, *, model: str, provider_name: str, model_prefix: str,
                 api_key: str | None, api_key_env: str, base_url: str | None = None,
                 timeout: float = 120.0, max_output_tokens: int = 8192,
                 native_json_schema: bool = False) -> None:
        super().__init__()
        if max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        self._model = model
        self.provider_name = provider_name
        self.model_prefix = model_prefix
        self.api_key_env = api_key_env
        self._api_key = api_key or os.environ.get(api_key_env)
        self._base_url = base_url.rstrip("/") if base_url else None
        self._timeout = timeout
        self._max_output_tokens = max_output_tokens
        self._native_json_schema = native_json_schema

    def _sdk_model(self, model: str) -> str:
        return model if model.startswith(self.model_prefix + "/") else f"{self.model_prefix}/{model}"

    def schema_transport(self, model: str | None = None) -> Literal["native", "prompt"]:
        """Return how ``json_schema`` is sent for the effective hosted model.

        The answer is intentionally public so callers can explain or validate the
        provider capability without duplicating SDK wire knowledge. Providers with a
        model-specific capability (currently Groq) override this method.
        """
        del model
        return "native" if self._native_json_schema else "prompt"

    def _schema_request(self, system: str, *, json_mode: bool,
                        json_schema: dict | None,
                        native_json_schema: bool | None = None) -> tuple[str, dict | None]:
        if json_schema is None:
            return system, ({"type": "json_object"} if json_mode else None)
        if self._native_json_schema if native_json_schema is None else native_json_schema:
            return system, {"type": "json_schema", "json_schema": {
                "name": "completion", "strict": True, "schema": json_schema,
            }}
        # Explicitly choose JSON object mode where the selected backend has no native
        # schema transport. Application-level validation remains authoritative.
        return system_with_schema(system, json_schema), {"type": "json_object"}

    async def complete(self, prompt: str, *, system: str = "", json_mode: bool = False,
                       cls: Class = Class.BATCH, pin_model: bool = False,
                       model: str | None = None, json_schema: dict | None = None,
                       max_output_tokens: int | None = None) -> Completion:
        return await self._complete_hosted(
            prompt, system=system, json_mode=json_mode, cls=cls, pin_model=pin_model,
            model=model, json_schema=json_schema, max_output_tokens=max_output_tokens,
            native_json_schema=self._native_json_schema)

    async def _complete_hosted(self, prompt: str, *, system: str = "", json_mode: bool = False,
                       cls: Class = Class.BATCH, pin_model: bool = False,
                       model: str | None = None, json_schema: dict | None = None,
                       max_output_tokens: int | None = None,
                       native_json_schema: bool | None = None) -> Completion:
        del cls  # LiteLLM has no priority concept; the provider boundary still carries it.
        use_model = model or self._model
        system, response_format = self._schema_request(system, json_mode=json_mode,
                                                        json_schema=json_schema,
                                                        native_json_schema=native_json_schema)
        messages = ([{"role": "system", "content": system}] if system else [])
        messages.append({"role": "user", "content": prompt})
        kwargs: dict[str, Any] = {
            "model": self._sdk_model(use_model),
            "messages": messages,
            "max_tokens": max_output_tokens if max_output_tokens is not None else self._max_output_tokens,
            "timeout": self._timeout,
            "num_retries": 0,
            "fallbacks": [],
        }
        if response_format is not None:
            kwargs["response_format"] = response_format
        if self._api_key:
            kwargs["api_key"] = self._api_key
        if self._base_url:
            kwargs["api_base"] = self._base_url
        try:
            if litellm is None:
                raise RuntimeError("LiteLLM is required for hosted providers")
            response = litellm.acompletion(**kwargs)
            if inspect.isawaitable(response):
                response = await response
        except BaseException as exc:
            # Cancellation is intentionally not normalized: callers must be able to
            # cancel a request and let the SDK close its in-flight HTTP operation.
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            self._raise_normalized(exc, schema=json_schema is not None)
            raise AssertionError("_raise_normalized always raises")
        return self._completion(response, requested_model=use_model, pin_model=pin_model)

    def _raise_normalized(self, exc: BaseException, *, schema: bool) -> None:
        status = _status_from_error(exc)
        name = type(exc).__name__.lower()
        if status == 429 or "ratelimit" in name or "rate_limit" in name:
            delay, exact_hint, safe = _retry_delay(exc, fallback=30.0)
            raise AdmissionRejected("quota_exhausted" if status and _quota_category(exc, status) == "quota_exhausted"
                                    else "rate_limited", retry_after_s=delay,
                                    exact_hint=exact_hint,
                                    category=_quota_category(exc, status or 429),
                                    rate_limits=safe) from exc
        if status in _RETRYABLE_SERVER_STATUS:
            delay, exact_hint, safe = _retry_delay(exc, fallback=_SERVER_RETRY_S)
            raise AdmissionRejected("model_server_error", retry_after_s=delay,
                                    exact_hint=exact_hint, category="model_server_error",
                                    rate_limits=safe) from exc
        if status == 413 or any(marker in name for marker in
                                ("contextwindow", "context_length", "requesttoolong")):
            raise RequestBudgetExceeded("provider request exceeds context budget") from exc
        code, message = _structured_error(exc)
        schema_error = (code in _SCHEMA_ERROR_CODES or
                        ("unsupported" in message and
                         any(marker in message for marker in ("schema", "response_format", "json"))) or
                        ("schema" in message and "not supported" in message))
        if schema and (schema_error or any(marker in name for marker in
                                           ("unsupportedparam", "schemanotsupported"))):
            raise UnsupportedSchema("provider does not support the requested JSON schema") from exc
        if status is None and (name in _NETWORK_ERROR_NAMES or
                               any(name.endswith(marker) for marker in _NETWORK_ERROR_NAMES)):
            raise AdmissionRejected("unreachable", retry_after_s=_SERVER_RETRY_S,
                                    category="unreachable") from exc
        raise exc

    def _completion(self, response: Any, *, requested_model: str,
                    pin_model: bool = False) -> Completion:
        choices = _field(response, "choices", []) or []
        if not choices:
            raise RuntimeError("hosted provider returned no choices")
        choice = choices[0]
        finish = _field(choice, "finish_reason") or _field(response, "stop_reason")
        if finish in {"length", "max_tokens", "content_filter"}:
            raise TruncatedOutput("provider output was truncated before completion")
        message = _field(choice, "message", {}) or {}
        text = _field(message, "content", "")
        if text is None:
            text = ""
        if not isinstance(text, str):
            text = str(text)
        usage = _field(response, "usage", {}) or {}
        details = _field(usage, "prompt_tokens_details", {}) or {}
        cache_read = (_field(usage, "cache_read_input_tokens", 0) or
                      _field(usage, "prompt_cache_hit_tokens", 0) or
                      _field(details, "cached_tokens", 0) or 0)
        cache_write = (_field(usage, "cache_creation_input_tokens", 0) or
                       _field(usage, "prompt_cache_write_tokens", 0) or 0)
        completion_tokens = int(_field(usage, "completion_tokens", 0) or 0)
        explicit_output = max(
            completion_tokens,
            int(_field(usage, "output_tokens", 0) or 0),
            completion_tokens + int(_field(usage, "reasoning_tokens", 0) or 0),
            completion_tokens + int(_field(usage, "thinking_tokens", 0) or 0),
        )
        total_implied = int(_field(usage, "total_tokens", 0) or 0) - int(
            _field(usage, "prompt_tokens", 0) or 0)
        hidden = _field(response, "_hidden_params", {}) or {}
        served_provider = (_field(hidden, "custom_llm_provider", None) or
                           _field(response, "served_provider", None) or self.provider_name)
        served_model = (_field(response, "model", None) or
                        _field(hidden, "model_id", None) or requested_model)
        if isinstance(served_provider, str) and served_provider.startswith("litellm"):
            served_provider = self.provider_name
        rate_limits = _headers(response)
        hidden_headers = _field(hidden, "additional_headers", {}) or {}
        rate_limits.update(_headers({"headers": hidden_headers}))
        completion = Completion(
            text=text, served_provider=str(served_provider), served_model=str(served_model),
            input_tokens=int(_field(usage, "prompt_tokens", 0) or 0),
            output_tokens=max(explicit_output, total_implied, 0),
            cache_read_tokens=int(cache_read), cache_write_tokens=int(cache_write),
            rate_limits=rate_limits,
        )
        if pin_model and self._pin_requested_model(requested_model, completion):
            raise PinnedModelChanged("provider changed a pinned model")
        return completion

    def _pin_requested_model(self, requested_model: str, completion: Completion) -> bool:
        """Hook used by ``complete`` after provenance has been extracted."""
        served_model = completion.served_model
        if served_model.startswith(self.model_prefix + "/"):
            served_model = served_model[len(self.model_prefix) + 1:]
        return (completion.served_provider != self.provider_name or
                served_model != requested_model)

    async def embed(self, texts: list[str], *, cls: Class = Class.BATCH) -> list[list[float]]:
        raise NotImplementedError("Hosted completion providers do not provide embeddings")

    async def aclose(self) -> None:
        # LiteLLM owns its HTTP client lifecycle. Keep a uniform async lifecycle hook.
        return None
