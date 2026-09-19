import assert from "node:assert/strict";
import test from "node:test";
import { chapterStatusState, retriesExhausted, retryCategoryLabel, failureExplanation, scheduledRetryMessage } from "../src/factsStatus.ts";
import type { FactsStatus } from "../src/types.ts";

function status(patch: Partial<FactsStatus> = {}): FactsStatus {
  return { state: "failed", ...patch };
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

test("a chapter is ready once FACTS has run, and says how many facts it found", () => {
  assert.deepEqual(chapterStatusState(status({ state: "ready", facts_count: 15 })),
    { tone: "quiet", text: "Ready · 15 facts for the wiki", retryable: false });
  assert.equal(chapterStatusState(status({ state: "ready", facts_count: 1 })).text, "Ready · 1 fact for the wiki");
  assert.equal(chapterStatusState(status({ state: "pending" })).text, "Waiting to find names and facts…");
  assert.equal(chapterStatusState(status({ state: "processing" })).tone, "live");
  assert.equal(chapterStatusState(null).text, "Checking…");
});

test("only states a reader can act on offer a retry", () => {
  assert.equal(chapterStatusState(status({ state: "failed", retry_attempts: 5, retry_max_attempts: 5 })).retryable, true);
  assert.equal(chapterStatusState(status({ discarded: true })).retryable, true);
  assert.equal(chapterStatusState(status({ retry_at: "2099-01-01T00:00:00Z", retry_attempts: 1, retry_max_attempts: 5 })).retryable, false);
});
