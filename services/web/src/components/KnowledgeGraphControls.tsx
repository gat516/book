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
      ? "Nothing new to build: every readable chapter is ready or already being worked on."
      : null;
  });
  const stop = () => act(async () => {
    const result = await stopRecordsBuild(novelId);
    return `Paused. ${result.chapters_stopped} unfinished chapter${result.chapters_stopped === 1 ? "" : "s"} stopped; finished reader features are unchanged.`;
  });
  const rebuild = () => act(async () => { await rebuildRecords(novelId); return null; });
  const discard = () => act(async () => {
    if (status?.active_generation_id) await discardRecordsRebuild(novelId, status.active_generation_id);
    return "Refresh cancelled. The previous character cards, timeline details, and AskAI context are restored.";
  });

  const eligible = status?.eligible_chapters ?? 0;
  const published = status?.published_chapters ?? 0;
  const complete = !!status?.active_generation_id && eligible > 0 && status.missing_chapters === 0;
  const tone = status?.running ? "live" : status && !complete && status.active_generation_id ? "warn" : "quiet";
  return <section className="chapter-knowledge-graph" aria-label="Reader features status and controls">
    <div className="graph-build-status" role="status" aria-live="polite">
      <div className="reader-features-heading">
        <div>
          <strong>{chapter === undefined ? "Reader features" : "Reader features across this book"}</strong>
          <p>After a chapter is readable, the app finds its characters, facts, relationships, and events.</p>
        </div>
        <span className={`status-pill status-pill-${tone}`}>
          {tone === "live" && <span className="reader-records-dot" aria-hidden="true" />}
          {status ? graphCoverageLabel(status) : "Checking…"}
        </span>
      </div>
      {status && eligible > 0 && <>
        <progress max={eligible} value={published} aria-label={`Reader features ready for ${published} of ${eligible} chapters`} />
        <small>These details power character cards, the timeline, and AskAI. Chapter text is never changed.</small>
      </>}
    </div>
    {chapter !== undefined && <div className="knowledge-actions">
      <strong>Chapter {chapter}</strong>
      {chapterStatus?.extraction_status === "processing"
        ? <button disabled={busy} onClick={() => void act(async () => { await discardRecordsChapter(novelId, chapter); return "Reader-feature work paused for this chapter. You can resume it here."; })}>Pause this chapter</button>
        : <button disabled={busy || !chapterStatus || chapterStatus.extraction_status === "ready"}
            onClick={() => void act(async () => { await retryRecords(novelId, chapter); return "Reader-feature work queued. Earlier chapters must finish first."; })}>
            {chapterStatus?.extraction_status === "ready" ? "Reader features ready" : chapterStatus?.extraction_status === "failed" ? "Retry reader features" : "Build reader features for this chapter"}
          </button>}
    </div>}
    {status?.running
      ? <button type="button" disabled={busy} onClick={() => void stop()}>{busy ? "Pausing…" : "Pause reader-feature work"}</button>
      : <button type="button" disabled={busy || !status || complete} onClick={() => void extract()}>
          {busy ? "Starting…" : complete ? "Reader features are up to date" : "Build missing reader features"}
        </button>}
    <details className="graph-advanced">
      <summary>Advanced reader-feature options</summary>
      {confirm === null && <button type="button" disabled={busy || !status?.active_generation_id} onClick={() => setConfirm("rebuild")}>Refresh every chapter…</button>}
      {status?.has_predecessor && status.discardable && confirm === null && <button type="button" disabled={busy} onClick={() => setConfirm("discard")}>Cancel refresh…</button>}
      {confirm === "rebuild" && <span role="alert" className="graph-confirm">
        <small>Rebuild the character cards, timeline details, and AskAI context for every chapter. Existing reader features disappear while the refresh runs, but saved chapter text is unchanged. Use this after changing the story-details model.</small>
        <button type="button" className="btn-danger" disabled={busy} onClick={() => void rebuild()}>Refresh every chapter</button>
        <button type="button" disabled={busy} onClick={() => setConfirm(null)}>Cancel</button>
      </span>}
      {confirm === "discard" && <span role="alert" className="graph-confirm">
        <small>Cancel this refresh and restore the previous character cards, timeline details, and AskAI context.</small>
        <button type="button" className="btn-danger" disabled={busy} onClick={() => void discard()}>Restore previous reader features</button>
        <button type="button" disabled={busy} onClick={() => setConfirm(null)}>Cancel</button>
      </span>}
    </details>
    {notice && <small role="status" className="graph-notice">{notice}</small>}
    {error && <p role="alert" className="graph-error">Reader-feature controls unavailable: {error} <button type="button" onClick={() => void load()}>Retry status</button></p>}
  </section>;
}
