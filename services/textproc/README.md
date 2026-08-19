# textproc

Rust gRPC service for spoiler-engine text operations. `ScanMentions` uses cached
leftmost-longest Aho-Corasick matchers and returns UTF-8 byte plus Unicode-character
offsets. `HashContent` returns exact SHA-256 and a versioned near-duplicate SimHash.

Run with Docker from the repository root:

```bash
docker compose -f deploy/docker-compose.yml up --build textproc
```

The container health check calls the service's standard gRPC health endpoint. It can
also be run directly; `TEXTPROC_HEALTH_ADDR` defaults to
`http://127.0.0.1:50051`:

```bash
textproc --health-check
```

## Verification

Rust 1.88 in Docker is the canonical toolchain. The test target checks formatting,
strict Clippy, and the Rust suite:

```bash
make textproc-test
docker compose -f deploy/docker-compose.yml up -d --build textproc
make textproc-live-test
make textproc-benchmark
```

The live suite checks health reporting, the shared Rust/Python scan goldens, hashing,
request limits, the unimplemented retro-update response, and the pipeline scan stage
through both configured backends. `TEXTPROC_TEST_ADDR` can override the default
`127.0.0.1:50051` used by the Make targets.

## Benchmark

The deterministic benchmark builds 3,002 aliases and scans an 11,500-character mixed
CJK/Latin chapter. Outputs must be identical; latency is informational and has no CI
threshold. The construction estimate is cold-request latency minus median warm-request
latency, so it includes a small amount of request-level noise.

Observed on 2026-08-17 using the local release container:

| Backend | Latency | Throughput |
|---|---:|---:|
| Python fallback (median) | 2.480 ms | 4,636,865 chars/s |
| Rust gRPC cold | 10.083 ms | 1,140,483 chars/s |
| Rust gRPC warm (median) | 4.131 ms | 2,783,598 chars/s |

Estimated automaton construction time: 5.952 ms. All three runs returned the same 750
spans.

`ApplyRetroUpdate` is deliberately unimplemented until Milestone 3.
