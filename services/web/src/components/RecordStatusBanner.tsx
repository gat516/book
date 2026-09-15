import type { RecordsStatus } from "../types";
import { retriesExhausted, retryCategoryLabel, retryTimeLabel } from "../recordStatus";

// The same tone vocabulary the processing-queue pill uses: green means work is in flight,
// amber means waiting on something, red means it failed. One colour language across both
// status surfaces, so a reader learns it once rather than per-panel.
type Tone = "live" | "warn" | "bad" | "quiet";

// Where the reader goes to act on a paused or failed chapter. Extraction is started from
// the book-wide graph controls, so the chapter bar names that button rather than offering
// one of its own.
const RESUME_HINT = "Use the Reader features controls below to resume.";

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
      ? { tone: "quiet", text: "Checking…" }
      // Ranked ahead of everything else because a discarded chapter has no run row, so
      // every status below would otherwise read it as "pending" and promise a worker that
      // is never coming. Discarding also clears the retry fields, so nothing below applies.
      : status.discarded
      ? { tone: "quiet", text: `Paused for this chapter. ${RESUME_HINT}` }
      : status.retry_at
      ? {
          tone: "warn",
          text: `Trying again automatically after ${category} (attempt ${attemptLabel}) · ${retryTimeLabel(status.retry_at)}`,
        }
      : retriesExhausted(status)
      ? {
          tone: "bad",
          text: `Couldn’t finish after ${attemptLabel} attempts because of ${category}. The chapter is still readable.${onRetry ? "" : ` ${RESUME_HINT}`}`,
          retryable: true,
        }
      : status.extraction_status === "failed"
      ? {
          tone: "bad",
          text: `Couldn’t build reader features. The chapter is still readable${status.failure_detail ? ` (${status.failure_detail})` : ""}.${onRetry ? "" : ` ${RESUME_HINT}`}`,
          retryable: true,
        }
      : status.extraction_status === "processing"
      ? { tone: "live", text: `Finding characters, facts, relationships, and events${attempts > 0 ? ` · attempt ${attemptLabel}` : ""}…` }
      : status.extraction_status === "pending"
      ? { tone: "live", text: "Waiting to find this chapter’s story details…" }
      : status.rendering_status === "failed"
      ? { tone: "bad", text: "Story details were found, but couldn’t be prepared for display. The chapter is still readable." }
      : status.rendering_status === "pending"
      ? { tone: "live", text: "Preparing story details for character cards, timeline, and AskAI…" }
      // The resting state. Extraction previously rendered nothing once it succeeded, so a
      // healthy chapter and a chapter whose knowledge had never been requested looked
      // identical -- both simply had no bar.
      : {
          tone: "quiet",
          text: status.generation_id === null
            ? "Not built for this chapter yet."
            : status.warning_count > 0
              ? `Ready, with ${status.warning_count} detail${status.warning_count === 1 ? "" : "s"} skipped.`
              : "Ready — character cards, timeline, and AskAI can use this chapter.",
        };

  return (
    <p
      // A failure interrupts; progress does not. Keeping alert for the red states only
      // stops a screen reader announcing every routine poll.
      role={state.tone === "bad" ? "alert" : "status"}
      className="reader-records-status"
    >
      <strong className="reader-records-label">This chapter’s reader features</strong>
      <span className={`status-pill status-pill-${state.tone}`}>
        {state.tone === "live" && <span className="reader-records-dot" aria-hidden="true" />}
        {state.text}
      </span>
      {/* The bar in "status bar": an indeterminate progress track while this chapter is
          queued or being extracted, so live work is visible at a glance and not just as
          a line of text. Removed as soon as the chapter settles. */}
      {state.tone === "live" && (
        <progress className="reader-records-progress" aria-label="Reader features are being prepared" />
      )}
      {/* Chapters publish in order, so a retry behind an unextracted chapter is refused
          (ingest-api returns 409). Say what it is waiting on instead of offering a button
          that cannot work. */}
      {state.retryable && onRetry && (status?.waiting_on_chapter != null
        ? <small className="reader-records-waiting">Waiting for story details from chapter {status.waiting_on_chapter} first.</small>
        : <button type="button" disabled={busy} onClick={onRetry}>{busy ? "Retrying…" : "Retry now"}</button>
      )}
    </p>
  );
}
