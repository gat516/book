# Deployed admission-only smoke — 2026-09-22

QiReadr's Ask AI and both pipeline replicas are now using the optional gateway
admission path for the approved account's DeepSeek completions. Private gateway
and separate persistent Redis are healthy. Gateway receives no provider keys,
prompts or prose; its completion/embedding proxy service is not registered.

The approved test made exactly **six paid Ask AI requests**, using the saved
`deepseek-v4-flash` model, existing reasoning/output settings and chapter-170 gate.
No chapter was enqueued or processed. Progress remained 170; chapter 176 remained
done and 177–180 remained ingested/untranslated. Public health stayed healthy.

| Measure | Result |
|---|---:|
| Successful Ask AI requests | 6/6 |
| Provider-limit errors | 0 |
| Ask AI HTTP median | 6.853 s |
| Ask AI HTTP range | 4.261–17.480 s |
| Reserve + Settle median overhead | 3.808 ms |
| Reserve + Settle maximum overhead | 5.363 ms |
| Successful settlements | 6/6 |
| Residual permits | 0 |
| Total reported input / output tokens | 9,396 / 11,078 |

All requests retrieved the same eight source identities, fingerprint
`21ead1ca41889717`, with 1,566 reported input tokens each. Output tokens varied
654–3,641. HTTP timing includes retrieval and completion but excludes the browser,
Cloudflare and reader API. Admission timing is the two RPCs, measured separately.

Before paid calls, deployed metadata-only reservations verified the shared cap:
two admitted, a third rejected, both settled, full capacity restored. The actual
pipeline provider factory resolved the admission wrapper under the saved account
credential, without making a provider call. This is wiring verification—not a
new background-chapter performance test. The two-request cap is an operator
guardrail; the hosted semaphore does not prioritize Ask AI over pipeline calls.

**No production speedup is demonstrated.** The earlier direct-path screen found
no contention. This smoke has no simultaneous chapter work or matched randomized
control, and output/reasoning token counts vary. The defensible result is working
shared admission at approximately 4 ms overhead, with all tested permits released.

Machine-readable samples, deployment digests, usage and timing events are in
`2026-09-22-gateway-deployed.json`. Branches are `feat/hosted-gateway-trial` and
the sibling's `feat/admission-only-service`; changes were uncommitted at the time
of this measurement. They were preserved on `main` on September 29 as book
`61cc88c` and gateway `65e97d8`.
The Python release contains only six integration files over deployed `ebd1d43`;
unrelated working-tree changes and private experiments were excluded.

Verification also passed the focused Python suite (171 tests; DB/network-dependent
cases excluded or skipped), all 38 non-DB worker tests with disposable Redis,
24 measurement/guard tests, the real Go-gateway/Python-client integration test,
the gateway's full Go unit suite, and Redis semaphore/hosted-controller race tests.
Only the disposable local test Redis container was removed afterward; production
gateway/Redis remain running. No paid calls beyond the approved six were made.

Rollback: remove the two admission env vars from askai/pipeline and restore Python
tag `ebd1d43`. Exact pre-rollout metadata and patches are retained on the node at
`/root/qireadr/gateway-trial-20260922`. Keep the gateway through the lease/sweep
drain before scaling down. See `deploy/hosted/GATEWAY_TRIAL.md`.

The trial created private ECR repository `private-books/llm-gateway` (immutable
tags, scan on push) and a 1 GiB local-path PVC; it did not provision a new EC2/RDS
instance. The ECR repository is not yet in the main Terraform resource set/state;
retain it for this trial and explicitly import it if adopting the integration.
