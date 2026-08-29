import { useCallback, useEffect, useState } from "react";
import { getChapterPreview, prioritizeChapter, putProgress, translateAhead } from "../api";
import { usePolling } from "../usePolling";
import { PipelineStatus } from "./PipelineStatus";
import { NameReviewPanel } from "./NameReviewPanel";

interface Props {
  novelId: string;
  chapterIndex: number;
  siteChapterNo?: string;
  onReady: () => void;
  onBack: () => void;
}

// Shown when the reader opens a chapter the pipeline hasn't finished translating yet.
// Rather than dumping a raw 404/409, this holds the reader here and polls until the
// chapter becomes readable, then hands off.
//
export function ChapterPending({ novelId, chapterIndex, siteChapterNo, onReady, onBack }: Props) {
  const [error, setError] = useState<string | null>(null);
  const [checks, setChecks] = useState(0);
  const [status, setStatus] = useState("");

  const [ready, setReady] = useState(false);
  const [requestingMore, setRequestingMore] = useState(false);
  const [preview, setPreview] = useState<string | null>(null);

  const [priorityNotice, setPriorityNotice] = useState("");
  const [initialRequestDone, setInitialRequestDone] = useState(false);

  // An explicit reader request takes precedence over background lookahead. An active
  // chapter is left alone; neither its model call nor other pending jobs are cancelled.
  useEffect(() => {
    let active = true;
    prioritizeChapter(novelId, chapterIndex).then((result) => {
      if (active) {
        setPriorityNotice(result.prioritized ? "Requested next after the current chapter finishes." : "Already processing or ready.");
        setInitialRequestDone(true);
      }
    }).catch((err) => { if (active) setError(String(err)); });
    return () => { active = false; };
  }, [novelId, chapterIndex]);

  // ONE poll answers everything this view needs: how far the translation has got, and
  // whether it's readable yet. This replaced a second timer that probed readiness by
  // retrying PUT /progress — a write, every few seconds, to answer a read-only question
  // (58 of them on one chapter). The single write now happens once, when it will succeed.
  const poll = useCallback(async () => {
    try {
      const latest = await getChapterPreview(novelId, chapterIndex);
      setError(null);
      setPreview(latest.available ? (latest.text ?? "") : null);
      setStatus(latest.status);
      setChecks((n) => n + 1);
      if (latest.status === "done") {
        await putProgress(novelId, chapterIndex);
        setReady(true); // stops the poll
        onReady();
      }
    } catch (err) {
      setError(String(err));
    }
    // onReady is intentionally excluded: the parent recreates it each render, and
    // depending on it would rebuild this callback (and restart the poll) every tick.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [novelId, chapterIndex]);

  useEffect(() => {
    if (initialRequestDone) void poll();
  }, [poll, initialRequestDone]);

  // Fast only while text is actually arriving — that's the only time a quick cadence buys
  // anything. Waiting through the earlier stages, which take minutes and show nothing,
  // polls slowly. Stops entirely once readable or failed.
  const streaming = preview !== null && status !== "done";
  const settled = ready || status === "error" || status === "needs_name_review" || error !== null;
  usePolling(poll, streaming ? 2000 : 6000, initialRequestDone && !settled && !requestingMore);

  async function requestMore(priority = false) {
    setRequestingMore(true);
    setError(null);
    try {
      if (priority) {
        const result = await prioritizeChapter(novelId, chapterIndex);
        setPriorityNotice(result.prioritized ? "Requested next after the current chapter finishes." : "Already processing or ready.");
      } else {
        await translateAhead(novelId, chapterIndex, 10);
      }
      setStatus("queued");
      setInitialRequestDone(true);
      setReady(false);
      await poll();
    } catch (err) {
      setError(String(err));
    } finally {
      setRequestingMore(false);
    }
  }

  return (
    <div className="chapter-pending">
      <h2>
        Chapter {chapterIndex}
        {siteChapterNo && <span className="chapter-pending-site"> — {siteChapterNo}</span>}
      </h2>
      {status === "needs_name_review" ? (
        <p>This chapter is paused until a character-name spelling is approved below.</p>
      ) : status === "error" ? (
        // Previously this view waited forever on a chapter that had already failed: the
        // old readiness probe couldn't tell "not ready yet" from "will never be ready".
        <p className="chapter-pending-error">
          This chapter failed during processing. Retry it with priority below. If it fails
          again, check the pipeline error; failures can come from the model or a service.
        </p>
      ) : (
        <p>This chapter is waiting for processing to finish. It will open automatically when it's ready.</p>
      )}
      {priorityNotice && <p role="status">{priorityNotice}</p>}
      <PipelineStatus novelId={novelId} />
      {status === "needs_name_review" && <NameReviewPanel novelId={novelId} chapter={chapterIndex} onApproved={() => {
        setStatus("queued");
        setReady(false);
        void poll();
      }} />}

      {/* Only shown while the TRANSLATE stage is actually streaming. Earlier stages emit
          JSON, not prose, so there is deliberately nothing to show during those — the
          status line above is what explains the wait then. */}
      {preview !== null && (
        <div className="chapter-preview">
          <p className="chapter-preview-label">Translating live — this is a draft and may still change:</p>
          <div className="chapter-preview-text">
            {preview}
            <span className="chapter-preview-cursor">▋</span>
          </div>
        </div>
      )}
      <p className="chapter-pending-hint">
        Checked {checks} time{checks === 1 ? "" : "s"}
        {status && ` · chapter status: ${status}`}
      </p>
      {error && <p className="chapter-pending-error">{error}</p>}
      <div className="chapter-pending-actions">
        <button onClick={onBack}>← Back to chapters</button>
        <button onClick={() => requestMore(true)} disabled={requestingMore}>
          {requestingMore ? "Requesting…" : status === "error" || error ? "Retry this chapter with priority" : "Prioritize this chapter"}
        </button>
        <button onClick={() => requestMore()} disabled={requestingMore}>
          {requestingMore ? "Queueing…" : "Translate 10 more from here"}
        </button>
      </div>
    </div>
  );
}
