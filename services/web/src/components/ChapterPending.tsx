import { useCallback, useEffect, useState } from "react";
import { getChapterPreview, putProgress, translateAhead } from "../api";
import { usePolling } from "../usePolling";
import { PipelineStatus } from "./PipelineStatus";

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
// Readiness is probed with putProgress rather than getChapter: advancing progress only
// succeeds against a chapter with status='done' (reader-api's AdvanceProgress), so a 409
// means "still translating" unambiguously — whereas getChapter would 404 for a chapter
// that merely sits above stored progress, indistinguishable from one that doesn't exist.
// It's also the call we'd have to make anyway to read the chapter, and it never moves
// progress backward (GREATEST), so polling it is safe.
export function ChapterPending({ novelId, chapterIndex, siteChapterNo, onReady, onBack }: Props) {
  const [error, setError] = useState<string | null>(null);
  const [checks, setChecks] = useState(0);
  const [status, setStatus] = useState("");

  const [ready, setReady] = useState(false);
  const [requestingMore, setRequestingMore] = useState(false);
  const [preview, setPreview] = useState<string | null>(null);

  // Translation follows the reader (novel.translate_lookahead), so a chapter reached by
  // jumping — past the end of that window — is not queued at all and would never arrive.
  // Requesting exactly this chapter on arrival is what makes waiting here terminate.
  useEffect(() => {
    translateAhead(novelId, chapterIndex, 1).catch(() => {
      /* best effort: the poll below still reports the real state */
    });
  }, [novelId, chapterIndex]);

  // ONE poll answers everything this view needs: how far the translation has got, and
  // whether it's readable yet. This replaced a second timer that probed readiness by
  // retrying PUT /progress — a write, every few seconds, to answer a read-only question
  // (58 of them on one chapter). The single write now happens once, when it will succeed.
  const poll = useCallback(async () => {
    try {
      const latest = await getChapterPreview(novelId, chapterIndex);
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
    poll();
  }, [poll]);

  // Fast only while text is actually arriving — that's the only time a quick cadence buys
  // anything. Waiting through the earlier stages, which take minutes and show nothing,
  // polls slowly. Stops entirely once readable or failed.
  const streaming = preview !== null && status !== "done";
  const settled = ready || status === "error" || error !== null;
  usePolling(poll, streaming ? 2000 : 6000, !settled);

  async function requestMore() {
    setRequestingMore(true);
    try {
      await translateAhead(novelId, chapterIndex, 10);
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
      {status === "error" ? (
        // Previously this view waited forever on a chapter that had already failed: the
        // old readiness probe couldn't tell "not ready yet" from "will never be ready".
        <p className="chapter-pending-error">
          Translation failed for this chapter. Requeue it below to try again — if it keeps
          failing, the model is likely rejecting it rather than the pipeline being stuck.
        </p>
      ) : (
        <p>This chapter is still being translated. It will open automatically when it's ready.</p>
      )}
      <PipelineStatus novelId={novelId} />

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
        <button onClick={requestMore} disabled={requestingMore}>
          {requestingMore ? "Queueing…" : "Translate 10 more from here"}
        </button>
      </div>
    </div>
  );
}
