# Optional hosted gateway trial

Implemented on `feat/hosted-gateway-trial`; requires the sibling gateway branch
`feat/admission-only-service`. The approved trial was deployed on 2026-09-22 and
passed all six paid smoke requests; see the [deployed report](../../eval/inference-contention/results/2026-09-22-gateway-deployed.md).
The procedure below is for reproducibility. No migrations are needed. Spec §14.7 is the
client contract; §15 ownership, BYOK and chapter gates remain mandatory.

## What changes

Ask AI and pipeline still call saved DeepSeek directly, after getting a shared
concurrency permit. Keys, prompts and responses never pass through the gateway.
Only explicitly listed accounts opt in. Other providers/embeddings stay direct.
The initial policy supports `deepseek-v4-flash` with two concurrent permits. Add
and validate a matching policy before switching that account to another model;
unknown models fail closed. The policy is account/model-scoped, not global across
different accounts that happen to share a provider key.

This does not lower the model's reasoning budget, change output quality, prioritize
Ask AI over already-running pipeline calls, or demonstrate a production speedup.
The earlier deployed screening experiment found no provider-limit errors.

## Verification

Run the focused Python suite and gateway's Go suite. The opt-in live test runs the
real Go service and Redis with a synthetic provider: no paid inference or chapter
content. Use a disposable Redis, never the application instance:

```sh
GATEWAY_TEST_BINARY=/path/to/gateway GATEWAY_TEST_REDIS_ADDR=127.0.0.1:16379 \
  services/pipeline/.venv/bin/python -m pytest -q -s eval/inference-contention/test_gateway_live.py
```

The test exercises shared capacity, rejection before provider invocation,
cancellation settlement, no remaining permits, and fail-closed gateway outage.
It prints only counts and reservation/settlement latency. Local sample results are
in `eval/inference-contention/results/2026-09-22-gateway-local.json`.

## Rollout (operator approval required)

1. Confirm the worker queue is idle and account controls unchanged. Record current
   askai/pipeline images and existing explicit env settings for exact rollback.
   Do not enqueue new chapters, change reader progress, or resume paused work.
2. Build immutable-tagged images from tracked sources plus **only** the reviewed
   integration changes. The current worktree has unrelated user edits and private
   experiments: do not build the full dirty tree. Use `Dockerfile.python` for
   QiReadr and the sibling's `Dockerfile` for the gateway. Push only approved
   images to the existing ECR registry, refreshing the node pull secret if needed.
3. Render the add-on with `gateway_trial.py --gateway-image REGISTRY/gateway:TAG
   --python-image REGISTRY/python:TAG --account ACCOUNT_UUID`. It produces only
   gateway/Redis resources; it does not replace the main app manifest. Save and
   review the YAML, then apply it to the existing `book` namespace via SSM.
4. Wait for gateway Redis and gateway readiness. Gateway health service
   `llmgw.v1.Admission` must be SERVING; `llmgw.v1.Gateway` must not be SERVING.
   Validate NetworkPolicies, PVC binding, resource headroom and actual lease 180s.
   The gateway has no external egress and its metrics port has no Service/Ingress.
5. Generate `--patch askai` and `--patch pipeline` separately using the same args.
   Use `kubectl patch deployment NAME --type=strategic --patch-file FILE`; these
   are patches, not complete deployments. They update only each named container's
   image and the two admission env vars. Preserve replicas, secrets, RLS, probes,
   service-link settings and all other environment values. Wait for each rollout.
6. First perform metadata-only Reserve/Settle/GetStatus checks from authorized app
   pods. Verify completion returns UNIMPLEMENTED on the keyless gateway. A real
   Ask AI smoke test requires approval for its paid provider calls/private content;
   keep it within saved chapter clearance and do not process a chapter implicitly.

## Metrics and interpretation

Retain only safe `gateway_admission event=...` log lines: reservation seconds,
provider seconds, settlement seconds, priority, success and settlement status.
Do not collect raw application exceptions or provider responses. Compare admitted
calls, rejections, unreachable gateway events, failed settlements and pending
reservations. Keep actual provider 429/quota errors separate from local admission
rejections. Existing scoped `account_usage` records retain provider/model/tokens.

Compare deployed Ask AI response latency separately from admission RPC overhead.
Keep context, chapter gate, model, reasoning settings and output budget fixed;
account for output-token variance. A six-request smoke test establishes function,
not p95/p99, throughput capacity or a speedup. A contention comparison needs its
own bounded workload approval and enough repeat runs to support the claim.

## Failure and rollback

While enabled, a gateway outage blocks new opted-in DeepSeek calls. Pipeline
defers durably without consuming provider retry attempts; Ask AI reports temporary
unavailability. There is deliberately no automatic bypass. Settlement failure
preserves successful output, logs a warning, and leaves at most a 180s lease.

To disable, remove `LLM_GATEWAY_ADMISSION_ADDR` and
`LLM_GATEWAY_ADMISSION_ACCOUNTS` from **both** deployments' explicit container env
and restore the recorded image tags; wait for readiness. If admission variables
were previously present, restore their recorded values instead of deleting them.
Do not overwrite the shared ConfigMap, credentials, or reader settings. Verify
direct calls and no new gateway events. Keep the gateway running for at least the
180s lease plus sweep interval before scaling it down. Retain its PVC until the
trial is reviewed; do not delete the application Redis or any chapter results.
