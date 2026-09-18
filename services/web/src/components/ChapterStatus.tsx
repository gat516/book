import { useState } from "react";
import { retryRecords } from "../api";
import { notifyKnowledgeUpdated } from "../knowledgeUpdates";
import { chapterStatusState } from "../recordStatus";
import type { RecordsStatus } from "../types";

/** One quiet line for this chapter's names and facts; it only gets loud when it fails. */
export function ChapterStatus({ novelId, chapter, status }: { novelId: string; chapter: number; status: RecordsStatus | null }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const state = chapterStatusState(status);

  async function retry() {
    setBusy(true);
    setError(null);
    try {
      await retryRecords(novelId, chapter);
      notifyKnowledgeUpdated(novelId);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  }

  return <p className={`chapter-status chapter-status-${state.tone}`} role={state.tone === "bad" ? "alert" : "status"}>
    <span className="chapter-status-dot" aria-hidden="true" />
    <span>{error ? `Retry failed: ${error}` : state.text}</span>
    {state.retryable && <button type="button" className="chapter-status-retry" disabled={busy} onClick={() => void retry()}>
      {busy ? "Retrying…" : state.text === "Paused" ? "Resume" : "Retry"}
    </button>}
  </p>;
}
