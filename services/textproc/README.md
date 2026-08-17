# textproc

Rust gRPC service for spoiler-engine text operations. `ScanMentions` uses cached
leftmost-longest Aho-Corasick matchers and returns UTF-8 byte plus Unicode-character
offsets. `HashContent` returns exact SHA-256 and a versioned near-duplicate SimHash.

Run with Docker from the repository root:

```bash
docker compose -f deploy/docker-compose.yml up --build textproc
```

`ApplyRetroUpdate` is deliberately unimplemented until Milestone 3.
