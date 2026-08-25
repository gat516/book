// No login system exists anywhere in this repo yet — reader-api's own README curl
// examples just hardcode a string. This is the accepted "fake principal" tradeoff from
// Phase 2 (PLAN.md), not an oversight: real auth slots in later without touching the
// gate logic. One random id per browser, persisted so progress survives a reload.
const KEY = "reader-id";

export function readerId(): string {
  let id = localStorage.getItem(KEY);
  if (!id) {
    id = crypto.randomUUID();
    localStorage.setItem(KEY, id);
  }
  return id;
}
