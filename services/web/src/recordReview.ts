export type ReviewDecision = "accepted" | "rejected";

export function reviewIntentKey(rowID: string, decision: ReviewDecision, reason: string): string {
  return `${rowID}\u0000${decision}\u0000${reason}`;
}

export function requestID(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") return crypto.randomUUID();
  return `review-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

/** Reuses an idempotency key while the exact intent is unresolved. */
export function requestIDForIntent(cache: Map<string, string>, key: string): string {
  const existing = cache.get(key);
  if (existing) return existing;
  const created = requestID();
  cache.set(key, created);
  return created;
}
