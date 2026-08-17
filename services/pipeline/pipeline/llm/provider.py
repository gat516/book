"""Compatibility import path for shared :mod:`novel_llm` provider contracts."""

from novel_llm.provider import (
    AdmissionRejected,
    BatchRequest,
    BatchResult,
    Class,
    Completion,
    LLMProvider,
    SequentialBatchMixin,
)

__all__ = [
    "AdmissionRejected", "BatchRequest", "BatchResult", "Class", "Completion",
    "LLMProvider", "SequentialBatchMixin",
]
