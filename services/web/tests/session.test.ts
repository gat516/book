import assert from "node:assert/strict";
import { test } from "node:test";
import { canUseLegacyLocalSession, setSession, sessionHeaders } from "../src/session.ts";

test("old local API works during development without weakening hosted authentication", () => {
  assert.equal(canUseLegacyLocalSession(true, "localhost", 404), true);
  assert.equal(canUseLegacyLocalSession(true, "127.0.0.1", 404), true);
  assert.equal(canUseLegacyLocalSession(false, "localhost", 404), false);
  assert.equal(canUseLegacyLocalSession(true, "books.example.com", 404), false);
  for (const status of [401, 403, 500]) assert.equal(canUseLegacyLocalSession(true, "localhost", status), false);
  setSession({ id: "old-reader", email: "Local", csrf_token: "", local: true, legacy: true });
  assert.deepEqual(sessionHeaders(), { "X-Reader-ID": "old-reader" });
  setSession({ id: "account", email: "Owner", csrf_token: "csrf" });
  assert.deepEqual(sessionHeaders(), { "X-CSRF-Token": "csrf" });
  setSession(null);
});
