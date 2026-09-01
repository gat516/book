// No login system exists anywhere in this repo yet — reader-api's own README curl
// examples just hardcode a string. This is the accepted "fake principal" tradeoff from
// Phase 2 (PLAN.md), not an oversight: real auth slots in later without touching the
// gate logic. One random id per browser, persisted so progress survives a reload.
const KEY = "reader-id";

function newReaderId(): string {
  // randomUUID is absent in older browsers and when the UI is opened through some
  // non-secure LAN origins. getRandomValues has much wider support, so construct the
  // same RFC 4122 v4 shape instead of making reader progress depend on randomUUID.
  const bytes = new Uint8Array(16);
  if (typeof globalThis.crypto?.getRandomValues === "function") {
    globalThis.crypto.getRandomValues(bytes);
  } else {
    // This ID is only the pre-auth reader-progress principal, not a secret or token.
    for (let index = 0; index < bytes.length; index += 1) {
      bytes[index] = Math.floor(Math.random() * 256);
    }
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;

  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0"));
  return `${hex.slice(0, 4).join("")}-${hex.slice(4, 6).join("")}-${hex.slice(6, 8).join("")}-${hex.slice(8, 10).join("")}-${hex.slice(10).join("")}`;
}

export function readerId(): string {
  let id = localStorage.getItem(KEY);
  if (!id) {
    id = newReaderId();
    localStorage.setItem(KEY, id);
  }
  return id;
}
