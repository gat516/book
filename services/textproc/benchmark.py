"""Deterministic Python-fallback versus cold/warm Rust gRPC benchmark."""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time

from pipeline.mentions import Alias, MentionScanRequest
from pipeline.textproc import GrpcTextProcClient, PythonTextProcClient


def fixture(alias_count: int = 3_000, repeats: int = 250) -> MentionScanRequest:
    aliases = [
        Alias(alias_id=f"entity-{index}", surface=f"Azure Sect {index}")
        for index in range(alias_count)
    ]
    aliases.extend(
        [
            Alias(alias_id="sect", surface="青云宗"),
            Alias(alias_id="hero", surface="Li Xiaoyao"),
        ]
    )
    paragraph = "李逍遥抵达青云宗。 Li Xiaoyao entered Azure Sect 2999. "
    return MentionScanRequest(text=paragraph * repeats, aliases=aliases, lang="zh")


async def timed(client, request: MentionScanRequest, runs: int) -> tuple[list[float], list]:
    durations: list[float] = []
    response = None
    for _ in range(runs):
        started = time.perf_counter()
        response = await client.scan(request)
        durations.append(time.perf_counter() - started)
    assert response is not None
    return durations, [span.model_dump() for span in response.spans]


async def main(address: str) -> None:
    request = fixture()
    python = PythonTextProcClient()
    grpc_client = GrpcTextProcClient(address, 30)
    try:
        python_times, python_spans = await timed(python, request, 5)
        cold_times, rust_spans = await timed(grpc_client, request, 1)
        warm_times, warm_spans = await timed(grpc_client, request, 10)
        assert python_spans == rust_spans == warm_spans
        chars = len(request.text)
        python_median = statistics.median(python_times)
        warm_median = statistics.median(warm_times)
        construction = max(0.0, cold_times[0] - warm_median)
        print(f"aliases={len(request.aliases)} chars={chars} spans={len(rust_spans)}")
        print(
            f"python median={python_median:.6f}s "
            f"throughput={chars / python_median:.0f} chars/s"
        )
        print(
            f"rust cold={cold_times[0]:.6f}s "
            f"construction estimate={construction:.6f}s "
            f"throughput={chars / cold_times[0]:.0f} chars/s"
        )
        print(
            f"rust warm median={warm_median:.6f}s "
            f"throughput={chars / warm_median:.0f} chars/s"
        )
    finally:
        await grpc_client.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--address", default="127.0.0.1:50051")
    arguments = parser.parse_args()
    asyncio.run(main(arguments.address))
