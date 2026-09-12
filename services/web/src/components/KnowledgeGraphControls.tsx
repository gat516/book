import { useCallback, useEffect, useState } from "react";
import { discardRecordsRebuild, extractRecords, getRecordsRebuildStatus, rebuildRecords, stopRecordsBuild } from "../api";
import type { RecordsRebuildStatus } from "../types";
import { usePolling } from "../usePolling";
import { graphCoverageLabel } from "../knowledgeLabels";

// The book's one graph control. Extraction is book-wide and chronological (who's-who
// resolves each chapter against the ones published before it), so a per-chapter start or
// discard button only offered a second, conflicting way to drive the same queue. The
// chapter view shows status; this is where work is started and stopped.
export function KnowledgeGraphControls({ novelId }: { novelId: string }) {
  const [status, setStatus] = useState<RecordsRebuildStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // Only the destructive, rarely-needed actions confirm; Extract and Stop are both safe
  // to press (stop is a pause, and extract never touches a published chapter).
  const [confirm, setConfirm] = useState<"rebuild" | "discard" | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    try { setStatus(await getRecordsRebuildStatus(novelId)); setError(null); }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
  }, [novelId]);
  useEffect(() => { void load(); }, [load]);
  usePolling(load, 8000, !!status?.running && error === null);

  async function act(run: () => Promise<string | null>) {
    setBusy(true); setError(null); setNotice(null);
    try { setNotice(await run()); setConfirm(null); await load(); }
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
    return "Rebuild discarded; the previous graph is back.";
  });

  const eligible = status?.eligible_chapters ?? 0;
  const published = status?.published_chapters ?? 0;
  const complete = !!status?.active_generation_id && eligible > 0 && status.missing_chapters === 0;
  return <section className="chapter-knowledge-graph" aria-label="Knowledge graph controls">
    <div className="graph-build-status" role="status" aria-live="polite">
      <strong>Knowledge graph</strong> <span>{status ? graphCoverageLabel(status) : "Loading graph status…"}</span>
      {status && <><progress max={Math.max(eligible, 1)} value={published} aria-label={`Knowledge graph progress: ${published} of ${eligible} chapters`} /><small>{published} of {eligible} chapters extracted{status.running ? " · extracting…" : ""}</small></>}
    </div>
    {status?.running
      ? <button type="button" disabled={busy} onClick={() => void stop()}>{busy ? "Stopping…" : "Stop extracting"}</button>
      : <button type="button" disabled={busy || !status || complete} onClick={() => void extract()}>
          {busy ? "Starting…" : complete ? "All chapters extracted" : "Extract facts for all chapters"}
        </button>}
    <details className="graph-advanced">
      <summary>Advanced</summary>
      {confirm === null && <button type="button" disabled={busy || !status?.active_generation_id} onClick={() => setConfirm("rebuild")}>Rebuild graph from scratch…</button>}
      {status?.has_predecessor && status.discardable && confirm === null && <button type="button" disabled={busy} onClick={() => setConfirm("discard")}>Discard unfinished rebuild…</button>}
      {confirm === "rebuild" && <span role="alert" className="graph-confirm">
        <small>Start a new graph and re-extract every chapter, including ones already done. Use this after changing the model or extraction settings. The current graph keeps serving until you discard the rebuild or it finishes.</small>
        <button type="button" className="btn-danger" disabled={busy} onClick={() => void rebuild()}>Confirm rebuild</button>
        <button type="button" disabled={busy} onClick={() => setConfirm(null)}>Cancel</button>
      </span>}
      {confirm === "discard" && <span role="alert" className="graph-confirm">
        <small>Throw away the unfinished rebuild and return to the previous graph.</small>
        <button type="button" className="btn-danger" disabled={busy} onClick={() => void discard()}>Confirm discard</button>
        <button type="button" disabled={busy} onClick={() => setConfirm(null)}>Cancel</button>
      </span>}
    </details>
    {notice && <small role="status" className="graph-notice">{notice}</small>}
    {error && <p role="alert" className="graph-error">Graph controls unavailable: {error} <button type="button" onClick={() => void load()}>Retry status</button></p>}
  </section>;
}
