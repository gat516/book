"""Async gRPC adapter for the sibling llm-inference-gateway service."""

from __future__ import annotations

import math

import grpc

from novel_llm import gateway_pb2, gateway_pb2_grpc
from novel_llm.provider import AdmissionRejected, Class, Completion, SequentialBatchMixin, system_with_schema


class GatewayProvider(SequentialBatchMixin):
    """A tenant-bound provider.  One instance is created for each novel."""

    def __init__(self, *, address: str, tenant: str, provider: str, model: str,
                 backend: str, embed_model: str, max_output_tokens: int = 8192,
                 timeout: float = 120.0) -> None:
        super().__init__()
        if not address or not tenant or not provider or not model:
            raise ValueError("gateway address, tenant, provider, and model are required")
        backend_map = {"hosted": gateway_pb2.HOSTED, "local_gpu": gateway_pb2.LOCAL_GPU}
        if backend not in backend_map:
            raise ValueError("gateway backend must be 'hosted' or 'local_gpu'")
        self._tenant, self._provider, self._model = tenant, provider, model
        self._backend = backend_map[backend]
        self._embed_model = embed_model
        self._max_output_tokens = max_output_tokens
        self._timeout = timeout
        self._channel = grpc.aio.insecure_channel(address)
        self._client = gateway_pb2_grpc.GatewayStub(self._channel)

    @staticmethod
    def _priority(cls: Class) -> int:
        return gateway_pb2.INTERACTIVE if cls is Class.INTERACTIVE else gateway_pb2.BATCH

    async def complete(self, prompt: str, *, system: str = "", json_mode: bool = False,
                       cls: Class = Class.BATCH, pin_model: bool = False,
                       model: str | None = None, json_schema: dict | None = None,
                       max_output_tokens: int | None = None) -> Completion:
        system = system_with_schema(system, json_schema)
        request = gateway_pb2.CompletionRequest(
            tenant=self._tenant, provider=self._provider, model=model or self._model,
            backend=self._backend, priority=self._priority(cls), system=system,
            messages=[gateway_pb2.CompletionMessage(role=gateway_pb2.USER, content=prompt)],
            max_output_tokens=max_output_tokens or self._max_output_tokens, no_fallback=pin_model,
            json_mode=json_mode or json_schema is not None,
        )
        text: list[str] = []
        served_provider = served_model = ""
        usage = (0, 0, 0, 0)
        try:
            call = self._client.Complete(request, timeout=self._timeout)
            async for chunk in call:
                if chunk.served_provider:
                    served_provider, served_model = chunk.served_provider, chunk.served_model
                if chunk.kind == gateway_pb2.TEXT:
                    text.append(chunk.text)
                elif chunk.kind == gateway_pb2.USAGE:
                    usage = (chunk.input_tokens, chunk.output_tokens,
                             chunk.cache_read_tokens, chunk.cache_write_tokens)
        except grpc.aio.AioRpcError as exc:
            self._raise_rpc(exc)
        if not served_provider or not served_model:
            raise RuntimeError("gateway stream ended without a served identity")
        return Completion("".join(text), served_provider, served_model, usage[0], usage[1], usage[2], usage[3])

    async def embed(self, texts: list[str], *, cls: Class = Class.BATCH) -> list[list[float]]:
        request = gateway_pb2.EmbedRequest(
            tenant=self._tenant, provider=self._provider, model=self._embed_model,
            backend=gateway_pb2.LOCAL_GPU, priority=self._priority(cls), inputs=texts,
        )
        try:
            reply = await self._client.Embed(request, timeout=self._timeout)
        except grpc.aio.AioRpcError as exc:
            self._raise_rpc(exc)
        return [list(vector.values) for vector in reply.embeddings]

    @staticmethod
    def _raise_rpc(exc: grpc.aio.AioRpcError) -> None:
        if exc.code() is grpc.StatusCode.RESOURCE_EXHAUSTED:
            delay = 0.0
            try:
                from grpc_status import rpc_status
                details = rpc_status.from_call(exc)
                for item in details.details if details else []:
                    if item.Is(__import__("google.rpc.error_details_pb2", fromlist=["RetryInfo"]).RetryInfo.DESCRIPTOR):
                        retry = __import__("google.rpc.error_details_pb2", fromlist=["RetryInfo"]).RetryInfo()
                        item.Unpack(retry)
                        delay = retry.retry_delay.seconds + retry.retry_delay.nanos / 1_000_000_000
            except Exception:  # Retry metadata is optional; admission is still distinct.
                pass
            raise AdmissionRejected(exc.details() or "gateway admission rejected", retry_after_s=delay) from exc
        raise RuntimeError(f"gateway {exc.code().name.lower()}: {exc.details()}") from exc

    async def aclose(self) -> None:
        await self._channel.close()
