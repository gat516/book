# Local runtime investigation — 2026-08-28

No Qwen inference or qualification benchmark was launched during this investigation.
The earlier Qwen report remains incomplete, not an accuracy failure. The original
graph remains untrusted; no revision was activated.

## What happened

- `KnowledgeEngine` hard-coded a 300-second HTTP timeout, ignoring the worker's
  `OLLAMA_TIMEOUT_SECONDS=900` configuration.
- Graph calls used `stream:false`, so the HTTP client received no incremental output.
- Ollama's journal shows Qwen loaded at 10:26:05 CDT, then cancelled at 10:30:58
  when `/api/chat` reached exactly five minutes. The runner had reached 2066 tokens
  (prompt plus generated output); it was not stuck loading. Late generation was
  around four tokens/second. The request's release record reported `truncated=0`.
- Normal translation was running concurrently, finishing a separate request at
  10:30:04 after 6m41s. Ollama reported three loaded runners during this period.
- Hardware: Core Ultra 7 256V, eight logical CPUs, about 15.1 GiB usable RAM,
  Intel Arc integrated GPU. Observed inference was CPU-only (`size_vram=0`).
- About 1.6 GiB of zram swap was occupied. This alone does not establish active
  thrashing or an out-of-memory failure. Ollama's service had no MemoryMax/CPUQuota
  cap, and the inspected journal showed client cancellation rather than an OOM.

## Changes

1. Graph extraction honors configured idle/prefill timeouts and has a separate
   30-minute total inference deadline. The live environment resolves to 900/1800s.
2. Internal JSON streaming receives progress without exposing partial claims to readers.
   Missing final records, stream errors and exhausted output caps fail closed.
3. Direct Book Ollama calls share a process-safe reservation. Benchmarks hold it across
   the complete run; translation defers without losing chapters and Ask AI reports
   temporary unavailability. This does not coordinate unrelated clients or a gateway.
4. Runtime settings participate in revision/cache identity. Successful calls retain
   load, prompt, generation, first-token and admission timings; stage failures are logged.
5. `--preflight` inspects runtime metadata and admission without loading/running Qwen.

## Verified without a Qwen run

The live preflight found Ollama 0.31.1, the expected installed Qwen digest, a 16K
context/4K output budget, 900-second idle and 1800-second total deadlines, and a free
Book reservation. No models were resident at that moment and about 10 GiB RAM was
available. This is a readiness observation, not a latency or quality score.

Regression coverage includes real HTTP streaming against a fake local server,
idle and total deadlines, incomplete output rejection, cross-process reservation,
cancellation/crash release, graph cache metrics, and resumable worker/graph backpressure.

Results: pipeline 179 passed / 1 skipped; shared provider package 26 passed;
Ask AI 5 passed. Migration 0028 was applied. With both chapter queues empty, the
worker and Ask AI were gracefully restarted and confirmed ready. Their startup
dimension checks used only the existing embedding model; Qwen was not loaded.

## Remaining limits

CPU inference can still take many minutes. Streaming prevents idle timeouts during
healthy generation; it does not increase tokens/second. Admission removes competing
Book inference but does not evict idle models or constrain unrelated clients.

The Intel GPU has not been enabled or benchmarked. A final journal check found the
exact reason: Ollama reports `dropping integrated GPU; to enable, set
OLLAMA_IGPU_ENABLE=1`. The installed Vulkan backend and Intel driver are present,
and the Ollama account already belongs to the video/render groups. The selected
service environment contains no explicit GPU override. Ollama documents Intel
support via its [Vulkan backend](https://docs.ollama.com/gpu).

Attempting to obtain administrator access returned `sudo: a password is required`.
No system service setting was changed. An optional temporary drop-in is prepared at
`deploy/ollama-book-runtime.conf`: enable the integrated GPU, allow one request at a
time and keep at most one model resident. The single-model cap is a memory constraint,
not a proven speed improvement; switching to embeddings will require model reloads.
Installing this file requires the owner's administrator authorization/password,
an idle Book/Ollama boundary, and a subsequent GPU detection check. GPU inference
speed/stability remain untested. No hosted provider was used or saved prose regenerated.

A future comparison must use the new runtime settings for both local models and
retain the existing precision/review gates. Runtime success alone cannot authorize
graph activation.
