import type { RecordsStatus } from "../types";
import { retriesExhausted, retryCategoryLabel, retryTimeLabel } from "../recordStatus";

// The same tone vocabulary the processing-queue pill uses: green means work is in flight,
// amber means waiting on something, red means it failed. One colour language across both
// status surfaces, so a reader learns it once rather than per-panel.
type Tone = "live" | "warn" | "bad" | "quiet";

// Where the reader goes to act on a paused or failed chapter. Extraction is started from
// the book-wide graph controls, so the chapter bar names that button rather than offering
// one of its own.
const RESUME_HINT = "Extract facts for all chapters resumes it.";

interface State {
  tone: Tone;
  text: string;
  // Only the two states a reader can actually act on offer the button. A scheduled retry
  // is already going to happen on its own, and a rendering failure needs a re-render
  // rather than a re-extract, so neither gets one.
  retryable?: boolean;
}

// `status` is nullable so the bar can render before the chapter's knowledge has loaded:
// this is an always-present surface, and a reader should never have to infer from an
// absent element whether extraction is fine, unstarted, or still being fetched.
export function RecordStatusBanner({ status, onRetry, busy = false }: {
  status: RecordsStatus | null;
  onRetry?: () => void;
  busy?: boolean;
}) {
  const attempts = status?.retry_attempts ?? 0;
  const max = status?.retry_max_attempts ?? 0;
  const attemptLabel = max > 0 ? `${attempts}/${max}` : `${attempts}`;
  const category = retryCategoryLabel(status?.retry_category);

  const state: State =
    status === null
      ? { tone: "quiet", text: "Checking chapter knowledge…" }
      // Ranked ahead of everything else because a discarded chapter has no run row, so
      // every status below would otherwise read it as "pending" and promise a worker that
      // is never coming. Discarding also clears the retry fields, so nothing below applies.
      : status.discarded
      ? { tone: "quiet", text: `Paused — extraction was stopped. ${RESUME_HINT}` }
      : status.retry_at
      ? {
          tone: "warn",
          text: `Automatic retry scheduled for ${category} (attempt ${attemptLabel}). Next attempt: ${retryTimeLabel(status.retry_at)}.`,
        }
      : retriesExhausted(status)
      ? {
          tone: "bad",
          text: `Automatic retries exhausted after ${attemptLabel} for ${category}. The chapter remains readable.${onRetry ? "" : ` ${RESUME_HINT}`}`,
          retryable: true,
        }
      : status.extraction_status === "failed"
      ? {
          tone: "bad",
          text: `Record extraction failed. The chapter remains readable${status.failure_detail ? ` (${status.failure_detail})` : ""}.${onRetry ? "" : ` ${RESUME_HINT}`}`,
          retryable: true,
        }
      : status.extraction_status === "processing"
      ? { tone: "live", text: `Extracting chapter records${attempts > 0 ? ` (attempt ${attemptLabel})` : ""}…` }
      : status.extraction_status === "pending"
      ? { tone: "live", text: "Queued for extraction — waiting for a free worker." }
      : status.rendering_status === "failed"
      ? { tone: "bad", text: "English record rendering failed. Source records remain available for review." }
      : status.rendering_status === "pending"
      ? { tone: "live", text: "Rendering records into English…" }
      // The resting state. Extraction previously rendered nothing once it succeeded, so a
      // healthy chapter and a chapter whose knowledge had never been requested looked
      // identical -- both simply had no bar.
      : {
          tone: "quiet",
          text: status.generation_id === null
            ? "No knowledge extracted for this chapter yet."
            : `Knowledge extracted for this chapter${status.warning_count > 0 ? ` · ${status.warning_count} warning${status.warning_count === 1 ? "" : "s"}` : ""}.`,
        };

  return (
    <p
      // A failure interrupts; progress does not. Keeping alert for the red states only
      // stops a screen reader announcing every routine poll.
      role={state.tone === "bad" ? "alert" : "status"}
      className="reader-records-status"
    >
      <strong className="reader-records-label">Chapter knowledge</strong>
      <span className={`status-pill status-pill-${state.tone}`}>
        {state.tone === "live" && <span className="reader-records-dot" aria-hidden="true" />}
        {state.text}
      </span>
      {/* The bar in "status bar": an indeterminate progress track while this chapter is
          queued or being extracted, so live work is visible at a glance and not just as
          a line of text. Removed as soon as the chapter settles. */}
      {state.tone === "live" && (
        <progress className="reader-records-progress" aria-label="Chapter extraction in progress" />
      )}
      {/* Chapters publish in order, so a retry behind an unextracted chapter is refused
          (ingest-api returns 409). Say what it is waiting on instead of offering a button
          that cannot work. */}
      {state.retryable && onRetry && (status?.waiting_on_chapter != null
        ? <small className="reader-records-waiting">Waiting on chapter {status.waiting_on_chapter} — extract earlier chapters first.</small>
        : <button type="button" disabled={busy} onClick={onRetry}>{busy ? "Retrying…" : "Retry now"}</button>
      )}
    </p>
  );
}
