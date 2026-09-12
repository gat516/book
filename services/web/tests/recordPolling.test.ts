import assert from "node:assert/strict";
import test from "node:test";
import { recordPollInterval, recordsTerminal, RECORD_PENDING_POLL_MS, RECORD_PROCESSING_POLL_MS } from "../src/recordPolling.ts";
import type { RecordsResponse } from "../src/types.ts";

function records(extraction_status: string, rendering_status = "pending"): RecordsResponse {
  return { novel_id: "novel", chapter_index: 1, at: 1, status: {
    generation_id: "generation", version: "generation:1", extraction_status, rendering_status, warning_count: 0,
  }, rows: [] };
}

test("records polling uses a slow pending cadence and faster active cadence", () => {
  assert.equal(recordPollInterval(null), RECORD_PENDING_POLL_MS);
  assert.equal(recordPollInterval(records("pending")), RECORD_PENDING_POLL_MS);
  assert.equal(recordPollInterval(records("processing")), RECORD_PROCESSING_POLL_MS);
  assert.equal(recordPollInterval(records("ready", "pending")), RECORD_PROCESSING_POLL_MS);
});

test("records polling terminates only at terminal extraction/rendering states", () => {
  assert.equal(recordsTerminal(records("pending")), false);
  assert.equal(recordsTerminal(records("processing")), false);
  assert.equal(recordsTerminal(records("ready", "pending")), false);
  assert.equal(recordsTerminal(records("ready", "ready")), true);
  assert.equal(recordsTerminal(records("ready", "failed")), true);
  assert.equal(recordsTerminal(records("failed")), true);
});

test("a scheduled retry keeps polling instead of being treated as terminal", () => {
  const value = records("failed");
  value.status.retry_at = "2099-01-01T00:00:00Z";
  value.status.retry_attempts = 2;
  value.status.retry_max_attempts = 5;
  assert.equal(recordsTerminal(value), false);
  assert.equal(recordPollInterval(value), RECORD_PENDING_POLL_MS);
});
