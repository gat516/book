import { useCallback, useEffect, useRef, useState } from "react";
import { extractRecords, getRecordsRebuildStatus, stopRecordsBuild } from "../api";
import { notifyKnowledgeUpdated, useKnowledgeRevision } from "../knowledgeUpdates";
import type { RecordsRebuildStatus } from "../types";
import { usePolling } from "../usePolling";
import { graphCoverageLabel } from "../knowledgeLabels";

// Book-wide names-and-facts progress. Per-chapter status lives in the reader header.
export function KnowledgeGraphControls({ novelId }: { novelId: string }) {
  const revision = useKnowledgeRevision(novelId);
  const previousStatus = useRef("");
  const [status, setStatus] = useState<RecordsRebuildStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
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
    try { setNotice(await run()); notifyKnowledgeUpdated(novelId); await load(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setBusy(false); }
  }
  const build = () => act(async () => {
    const result = await extractRecords(novelId);
    return result.chapters_enqueued === 0 ? "Every readable chapter already has its names and facts, or is being worked on." : null;
  });
  const pause = () => act(async () => {
    const result = await stopRecordsBuild(novelId);
    return `Paused ${result.chapters_stopped} unfinished chapter${result.chapters_stopped === 1 ? "" : "s"}. Finished chapters keep their names and facts.`;
  });

  const eligible = status?.eligible_chapters ?? 0;
  const ready = status?.published_chapters ?? 0;
  const complete = eligible > 0 && status?.missing_chapters === 0;
  return <section className="book-progress" aria-label="Names and facts across this book">
    <div className="book-progress-head">
      <strong>Names and facts</strong>
      <span role="status" aria-live="polite">{status ? graphCoverageLabel(status) : "Checking…"}</span>
      {status?.running
        ? <button type="button" disabled={busy} onClick={() => void pause()}>{busy ? "Pausing…" : "Pause"}</button>
        : !complete && <button type="button" disabled={busy || !status || eligible === 0} onClick={() => void build()}>{busy ? "Starting…" : "Find missing"}</button>}
    </div>
    {eligible > 0 && <progress max={eligible} value={ready} aria-label={`${ready} of ${eligible} chapters have names and facts`} />}
    {notice && <p role="status" className="book-progress-note">{notice}</p>}
    {error && <p role="alert" className="book-progress-error">Could not load progress: {error} <button type="button" onClick={() => void load()}>Retry</button></p>}
  </section>;
}
