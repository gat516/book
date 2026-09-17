import assert from "node:assert/strict";
import test from "node:test";
import { retriesExhausted, retryCategoryLabel, failureExplanation, scheduledRetryMessage } from "../src/recordStatus.ts";
import type { RecordsStatus } from "../src/types.ts";

function status(patch: Partial<RecordsStatus> = {}): RecordsStatus {
  return { generation_id: "g", version: "v", extraction_status: "failed", rendering_status: "pending", warning_count: 0, ...patch };
}

test("retry presentation distinguishes scheduled work from exhaustion", () => {
  assert.equal(retriesExhausted(status({ retry_attempts: 5, retry_max_attempts: 5 })), true);
  assert.equal(retriesExhausted(status({ retry_attempts: 5, retry_max_attempts: 5, retry_at: "2099-01-01T00:00:00Z" })), false);
  assert.equal(retryCategoryLabel("provider_http_429"), "the provider’s rate limit");
  assert.equal(retryCategoryLabel("unexpected_internal_name"), "a processing error");
});


test("processing errors identify the cause and next retry", () => {
  const retry = status({ retry_category: "provider_invalid_json",
    retry_attempts: 1, retry_max_attempts: 5, retry_at: "2099-01-01T00:00:00Z" });
  assert.equal(failureExplanation(retry), "The AI returned a response that could not be read.");
  assert.match(scheduledRetryMessage(retry), /1 of 5 attempts failed\. Next automatic retry:/);
  assert.equal(failureExplanation(status({ failure_detail: "secret source text" })),
    "An unexpected processing error occurred.");
});
