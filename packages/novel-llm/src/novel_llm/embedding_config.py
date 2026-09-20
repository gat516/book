"""Shared account embedding routing for offline publication and reader retrieval (§5.4).

Credentials are supplied by each service's existing decryptor. No chapter content is
read here; caller authorization and publication boundaries remain intact (§0).
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from novel_llm.accounts import hosted
import logging

from novel_llm.gemini import GeminiProvider
from novel_llm.openrouter import OpenRouterProvider
from novel_llm.provider import UnavailableEmbeddingProvider

DEFAULT_GEMINI_MODEL = "gemini-embedding-001"
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmbeddingBinding:
    provider: object
    space: str | None


def embedding_space(provider: str, model: str, dimensions: int, endpoint: str = "") -> str:
    # Keys may rotate without changing the vector space. Endpoints may serve a
    # different model under the same name, so their identity DOES belong here.
    return hashlib.sha256(json.dumps([provider, model.removeprefix("models/"), dimensions,
                                     endpoint.rstrip("/")]).encode()).hexdigest()


class EmbeddingResolver:
    def __init__(self, cfg, fallback, load_credential):
        self.cfg = cfg
        self.fallback = fallback
        self.load_credential = load_credential
        self.clients = {}
        self.disabled = EmbeddingBinding(UnavailableEmbeddingProvider(), None)

    async def resolve(self, conn) -> EmbeddingBinding:
        row = await (await conn.execute(
            "SELECT provider, model FROM embedding_config WHERE singleton"
        )).fetchone()
        provider, model = row if row else ("auto" if hosted() else "server", "")
        if hosted() and provider == "server":
            provider, model = "auto", ""
        server = provider == "server"
        if server:
            provider, model = self.cfg.embed_provider, self.cfg.embed_model
        if provider == "disabled":
            return self.disabled
        if provider == "auto":
            provider, model = "gemini", DEFAULT_GEMINI_MODEL
        if provider in {"gemini", "openrouter"}:
            try:
                _, key = await self.load_credential(conn, provider)
            except Exception as exc:
                # An unavailable optional search key must not prevent translation or
                # authorized answers from published knowledge. Never log key material.
                log.warning("embedding credential unavailable: %s", type(exc).__name__)
                return self.disabled
            key = key or ("" if hosted() else getattr(self.cfg, f"{provider}_api_key", ""))
            if not key:
                return self.disabled
            endpoint = (self.cfg.gemini_embed_base_url if provider == "gemini"
                        else self.cfg.openrouter_base_url) if server else (
                            "https://generativelanguage.googleapis.com/v1beta" if provider == "gemini"
                            else "https://openrouter.ai/api/v1")
            space = embedding_space(provider, model, self.cfg.embed_dim, endpoint)
            cache_key = (space, hashlib.sha256(key.encode()).hexdigest())
            if cache_key not in self.clients:
                kwargs = dict(model=model, embed_model=model, embed_dim=self.cfg.embed_dim, api_key=key)
                self.clients[cache_key] = (GeminiProvider(**kwargs, embed_base_url=endpoint)
                                           if provider == "gemini" else
                                           OpenRouterProvider(**kwargs, base_url=endpoint))
            return EmbeddingBinding(self.clients[cache_key], space)
        if provider not in {"ollama", "gateway"}:
            raise ValueError("unknown embedding provider")
        endpoint = self.cfg.ollama_host if provider == "ollama" else (
            f"{self.cfg.gateway_addr}/{self.cfg.gateway_provider}/{self.cfg.gateway_backend}")
        return EmbeddingBinding(self.fallback, embedding_space(provider, model, self.cfg.embed_dim, endpoint))

    async def aclose(self):
        for client in self.clients.values():
            await client.aclose()
        self.clients.clear()
