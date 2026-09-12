import assert from "node:assert/strict";
import test from "node:test";
import { requestIDForIntent, reviewIntentKey } from "../src/recordReview.ts";

test("review request ids are stable for an unresolved identical intent", () => {
  const cache = new Map<string, string>();
  const key = reviewIntentKey("row-1", "rejected", "not supported by passage");
  const first = requestIDForIntent(cache, key);
  assert.equal(requestIDForIntent(cache, key), first);
  assert.notEqual(requestIDForIntent(cache, reviewIntentKey("row-1", "accepted", "not supported by passage")), first);
  cache.delete(key);
  assert.notEqual(requestIDForIntent(cache, key), first);
});
