import { useEffect, useState } from "react";
import { ApiError, putProgress } from "../api";
import { PipelineStatus } from "./PipelineStatus";

interface Props {
  novelId: string;
  chapterIndex: number;
  siteChapterNo?: string;
  onReady: () => void;
  onBack: () => void;
}

const POLL_INTERVAL_MS = 4000;

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
  const [attempts, setAttempts] = useState(0);

  useEffect(() => {
    let cancelled = false;

    async function probe() {
      try {
        await putProgress(novelId, chapterIndex);
        if (!cancelled) onReady();
      } catch (err) {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 409) {
          setAttempts((n) => n + 1); // still translating — expected, keep waiting
          return;
        }
        setError(String(err));
      }
    }

    probe();
    const timer = setInterval(probe, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [novelId, chapterIndex]);

  return (
    <div className="chapter-pending">
      <h2>
        Chapter {chapterIndex}
        {siteChapterNo && <span className="chapter-pending-site"> — {siteChapterNo}</span>}
      </h2>
      <p>This chapter is still being translated. It will open automatically when it's ready.</p>
      <PipelineStatus novelId={novelId} />
      <p className="chapter-pending-hint">
        Checked {attempts} time{attempts === 1 ? "" : "s"}.
      </p>
      {error && <p className="chapter-pending-error">{error}</p>}
      <button onClick={onBack}>← Back to chapters</button>
    </div>
  );
}
