import assert from "node:assert/strict";
import { test } from "node:test";
import worker from "./worker.mjs";

const env = {
  PUBLIC_ORIGIN: "https://qireadr.com",
  ASSETS: { fetch: async () => new Response("public application") },
};

test("API mutations preserve cookie, CSRF, origin, body and query; strip forged identity", async t => {
  t.mock.method(globalThis, "fetch", async request => {
    assert.equal(request.url, "https://qireadr.com/api/novels?at=12");
    assert.equal(request.method, "POST");
    assert.equal(request.headers.get("Cookie"), "__Host-book-session=test");
    assert.equal(request.headers.get("Origin"), env.PUBLIC_ORIGIN);
    assert.equal(request.headers.get("X-CSRF-Token"), "test-csrf");
    assert.equal(request.headers.get("X-Account-ID"), null);
    assert.equal(request.headers.get("X-Reader-ID"), null);
    assert.equal(request.cache, "no-store");
    assert.equal(request.redirect, "manual");
    assert.equal(await request.text(), '{"title":"private"}');
    return new Response('{"id":"example"}', { status: 201 });
  });
  const response = await worker.fetch(new Request("https://qireadr.com/api/novels?at=12", {
    method: "POST",
    headers: {
      Cookie: "__Host-book-session=test", Origin: env.PUBLIC_ORIGIN,
      "X-CSRF-Token": "test-csrf", "X-Account-ID": "forged", "X-Reader-ID": "forged",
    },
    body: '{"title":"private"}',
  }), env);
  assert.equal(response.status, 201);
  assert.equal(await response.text(), '{"id":"example"}');
});

test("OAuth redirects preserve separate cookies and are not followed by the Worker", async t => {
  t.mock.method(globalThis, "fetch", async request => {
    assert.equal(request.redirect, "manual");
    const headers = new Headers({ Location: "https://accounts.google.com/o/oauth2/v2/auth" });
    headers.append("Set-Cookie", "__Host-book-login=test; Path=/; Secure; HttpOnly; SameSite=Lax");
    headers.append("Set-Cookie", "__Host-book-session=; Path=/; Secure; HttpOnly; Max-Age=0");
    return new Response(null, { status: 302, headers });
  });
  const response = await worker.fetch(new Request("https://qireadr.com/api/auth/login"), env);
  assert.equal(response.status, 302);
  assert.equal(response.headers.get("Location"), "https://accounts.google.com/o/oauth2/v2/auth");
  assert.equal(response.headers.getSetCookie().length, 2);
});

test("API data and authorization errors cannot acquire shared cache headers", async t => {
  for (const status of [200, 401, 403, 404, 429, 503]) {
    t.mock.method(globalThis, "fetch", async () => new Response("private or denied", {
      status, headers: { "Cache-Control": "public, max-age=3600", "CDN-Cache-Control": "public" },
    }));
    const response = await worker.fetch(new Request("https://qireadr.com/api/novels"), env);
    assert.equal(response.status, status);
    assert.equal(response.headers.get("Cache-Control"), "private, no-store");
    assert.equal(response.headers.get("CDN-Cache-Control"), "no-store");
    assert.equal(response.headers.get("Cloudflare-CDN-Cache-Control"), "no-store");
    assert.equal(await response.text(), "private or denied");
    t.mock.restoreAll();
  }
});

test("API responses stream without buffering the upstream body", async t => {
  let controller;
  const body = new ReadableStream({ start(value) { controller = value; } });
  t.mock.method(globalThis, "fetch", async () => new Response(body));
  const response = await worker.fetch(new Request("https://qireadr.com/api/novels/status"), env);
  controller.enqueue(new TextEncoder().encode("first"));
  const reader = response.body.getReader();
  assert.equal(new TextDecoder().decode((await reader.read()).value), "first");
  controller.close();
  assert.equal((await reader.read()).done, true);
});

test("network errors return a private generic response without upstream details", async t => {
  t.mock.method(globalThis, "fetch", async () => { throw new Error("private token and question"); });
  const response = await worker.fetch(new Request("https://qireadr.com/api/auth/session"), env);
  assert.equal(response.status, 502);
  assert.equal(response.headers.get("Cache-Control"), "private, no-store");
  assert.deepEqual(await response.json(), { error: "reader temporarily unavailable" });
});

test("unrelated paths use assets while unexpected hosts never reach the backend", async t => {
  t.mock.method(globalThis, "fetch", () => { throw new Error("must not proxy"); });
  assert.equal(await (await worker.fetch(new Request("https://qireadr.com/apiary"), env)).text(), "public application");
  assert.equal((await worker.fetch(new Request("https://other.example/api/novels"), env)).status, 421);
  const redirect = await worker.fetch(new Request("http://qireadr.com/api/auth/session"), env);
  assert.equal(redirect.status, 308);
  assert.equal(redirect.headers.get("Location"), "https://qireadr.com/api/auth/session");
});
