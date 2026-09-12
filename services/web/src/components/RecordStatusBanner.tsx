import type { RecordsStatus } from "../types";
import { retriesExhausted, retryCategoryLabel, retryTimeLabel } from "../recordStatus";

export function RecordStatusBanner({ status, onRetry, busy = false }: {
  status: RecordsStatus;
  onRetry?: () => void;
  busy?: boolean;
}) {
  const attempts = status.retry_attempts ?? 0;
  const max = status.retry_max_attempts ?? 0;
  const attemptLabel = max > 0 ? `${attempts}/${max}` : `${attempts}`;
  const category = retryCategoryLabel(status.retry_category);

  if (status.retry_at) {
    return <p role="status" className="reader-records-status">
      Automatic retry scheduled for {category} (attempt {attemptLabel}). Next attempt: {retryTimeLabel(status.retry_at)}.
    </p>;
  }
  if (retriesExhausted(status)) {
    return <p role="alert" className="reader-records-status reader-records-status-error">
      Automatic retries exhausted after {attemptLabel} for {category}. The chapter remains readable.
      {onRetry && <button type="button" disabled={busy} onClick={onRetry}>{busy ? "Retrying…" : "Retry now"}</button>}
    </p>;
  }
  if (status.extraction_status === "failed") {
    return <p role="alert" className="reader-records-status reader-records-status-error">
      Record extraction failed. The chapter remains readable{status.failure_detail ? ` (${status.failure_detail})` : ""}.
      {onRetry && <button type="button" disabled={busy} onClick={onRetry}>{busy ? "Retrying…" : "Retry now"}</button>}
    </p>;
  }
  if (status.extraction_status === "processing") {
    return <p role="status" className="reader-records-status">Extracting chapter records{attempts > 0 ? ` (attempt ${attemptLabel})` : ""}…</p>;
  }
  if (status.extraction_status === "pending") {
    return <p role="status" className="reader-records-status">Waiting for record extraction…</p>;
  }
  if (status.rendering_status === "failed") {
    return <p role="alert" className="reader-records-status reader-records-status-error">English record rendering failed. Source records remain available for review.</p>;
  }
  return null;
}
