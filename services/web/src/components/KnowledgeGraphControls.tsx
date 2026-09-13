import { useCallback, useEffect, useRef, useState } from "react";
import { discardRecordsRebuild, extractRecords, getRecordsRebuildStatus, rebuildRecords, stopRecordsBuild, retryRecords, discardRecordsChapter } from "../api";
import { notifyKnowledgeUpdated, useKnowledgeRevision } from "../knowledgeUpdates";
import type { RecordsStatus, RecordsRebuildStatus } from "../types";
import { usePolling } from "../usePolling";
import { graphCoverageLabel } from "../knowledgeLabels";

// Book and chapter extraction share the same chronological knowledge store.
export function KnowledgeGraphControls({ novelId, chapter, chapterStatus }: { novelId: string; chapter?: number; chapterStatus?: RecordsStatus }) {
  const revision = useKnowledgeRevision(novelId);
  const previousStatus = useRef("");
  const [status, setStatus] = useState<RecordsRebuildStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // Only the destructive, rarely-needed actions confirm; Extract and Stop are both safe
  // to press (stop is a pause, and extract never touches a published chapter).
  const [confirm, setConfirm] = useState<"rebuild" | "discard" | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const next = await getRecordsRebuildStatus(novelId);
      const signature = JSON.stringify(next);
      const changed = previousStatus.current !== "" && previousStatus.current !== signature;
      previousStatus.current = signature;
      setStatus(next); setError(null);
      if (changed) notifyKnowledgeUpdated(novelId);
    }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
  }, [novelId]);
  useEffect(() => { void load(); }, [load, revision]);
  usePolling(load, 8000, !!status?.running && error === null);

  async function act(run: () => Promise<string | null>) {
    setBusy(true); setError(null); setNotice(null);
    try { setNotice(await run()); notifyKnowledgeUpdated(novelId); setConfirm(null); await load(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setBusy(false); }
  }
  const extract = () => act(async () => {
    const result = await extractRecords(novelId);
    return result.chapters_enqueued === 0
      ? "Nothing new to extract: every readable chapter is either done or already being worked on."
      : null;
  });
  const stop = () => act(async () => {
    const result = await stopRecordsBuild(novelId);
    return `Stopped. ${result.chapters_stopped} unfinished chapter${result.chapters_stopped === 1 ? "" : "s"} paused; extracted chapters keep their facts.`;
  });
  const rebuild = () => act(async () => { await rebuildRecords(novelId); return null; });
  const discard = () => act(async () => {
    if (status?.active_generation_id) await discardRecordsRebuild(novelId, status.active_generation_id);
    return "Replacement cancelled; the previous facts, relationships, and events are restored.";
  });

  const eligible = status?.eligible_chapters ?? 0;
  const published = status?.published_chapters ?? 0;
  const complete = !!status?.active_generation_id && eligible > 0 && status.missing_chapters === 0;
  return <section className="chapter-knowledge-graph" aria-label="Knowledge graph controls">
    <div className="graph-build-status" role="status" aria-live="polite">
      <strong>Book knowledge</strong> <span>{status ? graphCoverageLabel(status) : "Loading knowledge status…"}</span>
      {status && <><progress max={Math.max(eligible, 1)} value={published} aria-label={`Knowledge graph progress: ${published} of ${eligible} chapters`} /><small>{published} of {eligible} chapters extracted{status.running ? " · extracting…" : ""}</small></>}
    </div>
    <p>Facts, relationships, and events power the reader’s cards and timeline, as well as AskAI.</p>
    {chapter !== undefined && <div className="knowledge-actions">
      <strong>Chapter {chapter}</strong>
      {chapterStatus?.extraction_status === "processing"
        ? <button disabled={busy} onClick={() => void act(async () => { await discardRecordsChapter(novelId, chapter); return "Chapter extraction stopped. You can resume it here."; })}>Stop this chapter</button>
        : <button disabled={busy || !chapterStatus || chapterStatus.extraction_status === "ready"}
            onClick={() => void act(async () => { await retryRecords(novelId, chapter); return "Chapter extraction queued. Earlier chapters must finish first."; })}>
            {chapterStatus?.extraction_status === "ready" ? "Chapter facts extracted" : chapterStatus?.extraction_status === "failed" ? "Retry this chapter" : "Extract facts for this chapter"}
          </button>}
    </div>}
    <p>Extract unfinished chapters in order. Previously extracted facts are kept.</p>
    {status?.running
      ? <button type="button" disabled={busy} onClick={() => void stop()}>{busy ? "Stopping…" : "Stop extracting"}</button>
      : <button type="button" disabled={busy || !status || complete} onClick={() => void extract()}>
          {busy ? "Starting…" : complete ? "All chapters extracted" : "Extract missing chapter facts"}
        </button>}
    <details className="graph-advanced">
      <summary>Replace existing facts…</summary>
      {confirm === null && <button type="button" disabled={busy || !status?.active_generation_id} onClick={() => setConfirm("rebuild")}>Re-extract all chapters…</button>}
      {status?.has_predecessor && status.discardable && confirm === null && <button type="button" disabled={busy} onClick={() => setConfirm("discard")}>Cancel replacement…</button>}
      {confirm === "rebuild" && <span role="alert" className="graph-confirm">
        <small>Replace the book’s facts, relationships, events, and identity links by extracting every chapter again. Existing knowledge disappears from reader views immediately; new results appear as chapters finish. Saved chapter text is unchanged. Use this after changing the model or extraction settings.</small>
        <button type="button" className="btn-danger" disabled={busy} onClick={() => void rebuild()}>Replace and re-extract all chapters</button>
        <button type="button" disabled={busy} onClick={() => setConfirm(null)}>Cancel</button>
      </span>}
      {confirm === "discard" && <span role="alert" className="graph-confirm">
        <small>Cancel this replacement and restore the previous facts, relationships, events, and cards.</small>
        <button type="button" className="btn-danger" disabled={busy} onClick={() => void discard()}>Restore previous knowledge</button>
        <button type="button" disabled={busy} onClick={() => setConfirm(null)}>Cancel</button>
      </span>}
    </details>
    {notice && <small role="status" className="graph-notice">{notice}</small>}
    {error && <p role="alert" className="graph-error">Knowledge controls unavailable: {error} <button type="button" onClick={() => void load()}>Retry status</button></p>}
  </section>;
}
