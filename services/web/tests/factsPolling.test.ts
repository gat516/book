import assert from "node:assert/strict";
import test from "node:test";
import { factsPollInterval, factsTerminal, FACTS_PENDING_POLL_MS, FACTS_PROCESSING_POLL_MS } from "../src/factsPolling.ts";
import type { FactsStatus } from "../src/types.ts";

const status = (state: string, patch: Partial<FactsStatus> = {}): FactsStatus => ({ state, ...patch });

test("facts polling uses a slow pending cadence and a faster active cadence", () => {
  assert.equal(factsPollInterval(null), FACTS_PENDING_POLL_MS);
  assert.equal(factsPollInterval(status("pending")), FACTS_PENDING_POLL_MS);
  assert.equal(factsPollInterval(status("processing")), FACTS_PROCESSING_POLL_MS);
});

test("facts polling stops only at ready, failed, or paused", () => {
  assert.equal(factsTerminal(status("pending")), false);
  assert.equal(factsTerminal(status("processing")), false);
  assert.equal(factsTerminal(status("ready")), true);
  assert.equal(factsTerminal(status("failed")), true);
  assert.equal(factsTerminal(status("pending", { discarded: true })), true);
});

test("a scheduled retry keeps polling instead of being treated as terminal", () => {
  const value = status("failed", { retry_at: "2099-01-01T00:00:00Z", retry_attempts: 2, retry_max_attempts: 5 });
  assert.equal(factsTerminal(value), false);
  assert.equal(factsPollInterval(value), FACTS_PENDING_POLL_MS);
});
