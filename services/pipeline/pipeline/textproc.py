"""Text processing backends for mention scanning and content hashing.

The Python implementation preserves the Phase-1 scanner for development. Production
selects the Rust service explicitly; gRPC failures are intentionally surfaced rather
than silently changing backend mid-chapter.
"""

from __future__ import annotations

from typing import Protocol

import grpc

from pipeline.mentions import MentionScanRequest, MentionScanResponse, Span, scan_mentions
from pipeline import textproc_pb2, textproc_pb2_grpc


class TextProcClient(Protocol):
    async def scan(self, request: MentionScanRequest) -> MentionScanResponse: ...

    async def aclose(self) -> None: ...


class PythonTextProcClient:
    async def scan(self, request: MentionScanRequest) -> MentionScanResponse:
        return scan_mentions(request)

    async def aclose(self) -> None:
        return None


class GrpcTextProcClient:
    def __init__(self, address: str, timeout_s: float) -> None:
        self._channel = grpc.aio.insecure_channel(address)
        self._stub = textproc_pb2_grpc.TextProcStub(self._channel)
        self._timeout_s = timeout_s

    async def scan(self, request: MentionScanRequest) -> MentionScanResponse:
        response = await self._stub.ScanMentions(
            textproc_pb2.MentionScanRequest(
                text=request.text,
                aliases=[textproc_pb2.Alias(alias_id=a.alias_id, surface=a.surface) for a in request.aliases],
                lang=request.lang,
            ),
            timeout=self._timeout_s,
        )
        return MentionScanResponse(
            spans=[
                Span(
                    alias_id=span.alias_id,
                    byte_start=span.byte_start,
                    byte_end=span.byte_end,
                    char_start=span.char_start,
                    char_end=span.char_end,
                )
                for span in response.spans
            ]
        )

    async def aclose(self) -> None:
        await self._channel.close()


def textproc_from_config(backend: str, address: str, timeout_s: float) -> TextProcClient:
    if backend == "python":
        return PythonTextProcClient()
    if backend == "grpc":
        return GrpcTextProcClient(address, timeout_s)
    raise ValueError(f"unknown TEXTPROC_BACKEND: {backend!r}")
