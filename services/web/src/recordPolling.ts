import type { RecordsResponse } from "./types";

// Extraction can spend minutes in a local model before a run is published. Pending is
// therefore a healthy waiting state and gets a slower check; active extraction/rendering
// gets a tighter cadence. Errors are handled by the caller and disable polling until the
// reader explicitly retries.
export const RECORD_PENDING_POLL_MS = 20000;
export const RECORD_PROCESSING_POLL_MS = 8000;

export function recordsTerminal(records: RecordsResponse | null): boolean {
  if (!records) return false;
  // A failed attempt with a retry_at is still live work. Stopping here makes a
  // healthy long-running local-model retry look permanently broken to the reader.
  if (records.status.retry_at) return false;
  if (records.status.extraction_status === "failed") return true;
  const extractionDone = records.status.extraction_status === "ready";
  const renderingDone = records.status.rendering_status === "ready" || records.status.rendering_status === "failed";
  return extractionDone && renderingDone;
}

export function recordPollInterval(records: RecordsResponse | null): number {
  if (!records) return RECORD_PENDING_POLL_MS;
  // Scheduled retries are intentionally checked slowly; the server owns the exact
  // retry schedule and pending can legitimately last several minutes.
  if (records.status.retry_at) return RECORD_PENDING_POLL_MS;
  return records.status.extraction_status === "processing" ||
    (records.status.extraction_status === "ready" && records.status.rendering_status === "pending")
    ? RECORD_PROCESSING_POLL_MS
    : RECORD_PENDING_POLL_MS;
}
