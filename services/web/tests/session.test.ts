import assert from "node:assert/strict";
import { test } from "node:test";
import { isLocalDevelopment, setSession, sessionHeaders } from "../src/session.ts";

test("old local API works during development without weakening hosted authentication", () => {
  assert.equal(isLocalDevelopment(true, "local"), true);
  assert.equal(isLocalDevelopment(true, undefined), true);
  assert.equal(isLocalDevelopment(true, "hosted"), false);
  assert.equal(isLocalDevelopment(false, "local"), false);
  assert.equal(isLocalDevelopment(false, undefined), false);
  setSession({ id: "old-reader", email: "Local", csrf_token: "", local: true, legacy: true });
  assert.deepEqual(sessionHeaders(), { "X-Reader-ID": "old-reader" });
  setSession({ id: "account", email: "Owner", csrf_token: "csrf" });
  assert.deepEqual(sessionHeaders(), { "X-CSRF-Token": "csrf" });
  setSession(null);
});
