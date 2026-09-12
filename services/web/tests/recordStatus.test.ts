import assert from "node:assert/strict";
import test from "node:test";
import { retriesExhausted, retryCategoryLabel } from "../src/recordStatus.ts";
import type { RecordsStatus } from "../src/types.ts";

function status(patch: Partial<RecordsStatus> = {}): RecordsStatus {
  return { generation_id: "g", version: "v", extraction_status: "failed", rendering_status: "pending", warning_count: 0, ...patch };
}

test("retry presentation distinguishes scheduled work from exhaustion", () => {
  assert.equal(retriesExhausted(status({ retry_attempts: 5, retry_max_attempts: 5 })), true);
  assert.equal(retriesExhausted(status({ retry_attempts: 5, retry_max_attempts: 5, retry_at: "2099-01-01T00:00:00Z" })), false);
  assert.equal(retryCategoryLabel("provider_http_429"), "provider http 429");
});
