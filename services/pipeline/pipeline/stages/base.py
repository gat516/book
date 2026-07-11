"""The ``Stage`` protocol every stage conforms to.

A stage is ``async def run(ctx, state) -> None`` that reads from ``ctx`` and mutates
``state``. The mutating-accumulator shape (rather than a typed transform per stage) is
what lets every stage ship as a no-op pass so the walking skeleton always runs — we
fill stages one at a time without inventing six I/O types up front (PLAN.md §1.4). The
signature is ``async`` even though pure stages never await, so the I/O-bound stages
(scan/resolve/translate/state) don't each bend the interface.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pipeline.context import PipelineState, StageContext


@runtime_checkable
class Stage(Protocol):
    name: str

    async def run(self, ctx: StageContext, state: PipelineState) -> None: ...
