import { useCallback, useEffect, useState } from "react";
import { discardRecordsRebuild, getRecordsRebuildStatus, rebuildRecords } from "../api";
import type { RecordsRebuildStatus } from "../types";
import { usePolling } from "../usePolling";
import { graphCoverageLabel } from "../knowledgeLabels";

export function KnowledgeGraphControls({ novelId }: { novelId: string }) {
  const [status, setStatus] = useState<RecordsRebuildStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirmDiscard, setConfirmDiscard] = useState(false);

  const load = useCallback(async () => {
    try { setStatus(await getRecordsRebuildStatus(novelId)); setError(null); }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
  }, [novelId]);
  useEffect(() => { void load(); }, [load]);
  usePolling(load, 8000, status?.has_predecessor === true && status.missing_chapters > 0 && error === null);

  async function rebuild() {
    setBusy(true); setError(null);
    try { await rebuildRecords(novelId); await load(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setBusy(false); }
  }
  async function discard() {
    if (!status?.active_generation_id) return;
    setBusy(true); setError(null);
    try { await discardRecordsRebuild(novelId, status.active_generation_id); setConfirmDiscard(false); await load(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setBusy(false); }
  }

  const eligible = status?.eligible_chapters ?? 0;
  const published = status?.published_chapters ?? 0;
  return <section className="chapter-knowledge-graph" aria-label="Knowledge graph controls">
    <div className="graph-build-status" role="status" aria-live="polite">
      <strong>Knowledge graph</strong> <span>{status ? graphCoverageLabel(status) : "Loading graph status…"}</span>
      {status && <><progress max={Math.max(eligible, 1)} value={published} aria-label={`Knowledge graph progress: ${published} of ${eligible} chapters`} /><small>{published} of {eligible} eligible chapters extracted</small></>}
    </div>
    <button type="button" disabled={busy} onClick={() => void rebuild()}>{busy ? "Starting graph…" : "Start / rebuild graph"}</button>
    {status?.has_predecessor && status.discardable && !confirmDiscard && <button type="button" disabled={busy} onClick={() => setConfirmDiscard(true)}>Discard unfinished graph</button>}
    {confirmDiscard && <span role="alert" className="graph-confirm">
      <small>Discard only the unfinished replacement; published generations remain immutable.</small>
      <button type="button" className="btn-danger" disabled={busy} onClick={() => void discard()}>Confirm discard</button>
      <button type="button" disabled={busy} onClick={() => setConfirmDiscard(false)}>Keep graph</button>
    </span>}
    {error && <p role="alert" className="graph-error">Graph controls unavailable: {error} <button type="button" onClick={() => void load()}>Retry status</button></p>}
  </section>;
}
