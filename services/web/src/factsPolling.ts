import type { FactsStatus } from "./types";

// FACTS can spend minutes in a model call. Pending is therefore a healthy waiting state
// and gets a slower check; active work gets a tighter cadence. Errors are handled by the
// caller and disable polling until the reader explicitly retries.
export const FACTS_PENDING_POLL_MS = 20000;
export const FACTS_PROCESSING_POLL_MS = 8000;

export function factsTerminal(status: FactsStatus | null): boolean {
  if (!status) return false;
  // A failed attempt with a retry_at is still live work. Stopping here makes a
  // healthy long-running retry look permanently broken to the reader.
  if (status.retry_at) return false;
  return status.state === "ready" || status.state === "failed" || !!status.discarded;
}

export function factsPollInterval(status: FactsStatus | null): number {
  if (!status || status.retry_at) return FACTS_PENDING_POLL_MS;
  return status.state === "processing" ? FACTS_PROCESSING_POLL_MS : FACTS_PENDING_POLL_MS;
}
