import { ProviderConfigPanel } from "./ProviderConfigPanel";
import { discardRecordsRebuild, getRecordsRebuildStatus, rebuildRecords } from "../api";
import type { RecordsRebuildStatus } from "../types";
import { useCallback, useEffect, useState } from "react";
import { usePolling } from "../usePolling";

interface Props {
  novelId: string;
  onClose: () => void;
}

// Per-book settings: provider/model configuration and knowledge repair. Both used to sit
// permanently at the top of the reader, polling and rendering on every page even while
// someone was just reading -- this is book-level, same as the reader's "← All chapters"
// boundary, not the account-level SettingsView.
export function BookSettingsView({ novelId, onClose }: Props) {
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<RecordsRebuildStatus | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [confirmDiscard, setConfirmDiscard] = useState(false);
  const [discardBusy, setDiscardBusy] = useState(false);

  const loadStatus = useCallback(async () => {
    try {
      const next = await getRecordsRebuildStatus(novelId);
      setStatus(next);
      setStatusError(null);
    } catch (reason) {
      setStatusError(reason instanceof Error ? reason.message : String(reason));
    }
  }, [novelId]);
  useEffect(() => { void loadStatus(); }, [loadStatus]);
  const rebuilding = status?.has_predecessor === true && status.active_state === "active" && !!status.active_generation_id && status.missing_chapters > 0;
  usePolling(loadStatus, 8000, rebuilding && !statusError);

  async function rebuild() {
    setBusy(true); setMessage(null); setError(null);
    try { const result = await rebuildRecords(novelId); setMessage(`Records rebuild started (${result.generation_id}).`); await loadStatus(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setBusy(false); }
  }
  async function discard() {
    if (!status?.active_generation_id) return;
    setDiscardBusy(true); setStatusError(null); setMessage(null);
    try {
      await discardRecordsRebuild(novelId, status.active_generation_id);
      setMessage("The unfinished records rebuild was discarded; the previous published generation remains active.");
      setConfirmDiscard(false);
      await loadStatus();
    } catch (reason) {
      setStatusError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setDiscardBusy(false);
    }
  }
  return (
    <section className="settings-view">
      <button className="app-back" onClick={onClose}>
        ← Back
      </button>
      <h2>Book settings</h2>

      <ProviderConfigPanel key={`provider-${novelId}`} novelId={novelId} />
      <section className="records-settings"><h3>Knowledge records</h3><p>Rebuild extraction in a new immutable generation when the ontology or extraction configuration changes.</p><button type="button" onClick={() => void rebuild()} disabled={busy}>{busy ? "Starting rebuild…" : "Rebuild records"}</button>
        {status?.has_predecessor && status.active_generation_id && <p role="status">Rebuild {status.active_state}: {status.published_chapters}/{status.eligible_chapters} chapters published ({status.missing_chapters} remaining).</p>}
        {statusError && <p role="alert" className="reader-pane-error">Could not load rebuild status: {statusError} <button type="button" onClick={() => void loadStatus()}>Retry status</button></p>}
        {status?.has_predecessor && status.discardable && status.active_generation_id && <div className="rebuild-discard">
          {!confirmDiscard ? <button type="button" onClick={() => setConfirmDiscard(true)}>Discard unfinished rebuild</button> : <>
            <p role="alert">Discard this unfinished rebuild? Its partial work will no longer be active; the previous published generation is kept.</p>
            <button type="button" disabled={discardBusy} onClick={() => void discard()}>{discardBusy ? "Discarding…" : "Confirm discard"}</button>
            <button type="button" disabled={discardBusy} onClick={() => setConfirmDiscard(false)}>Keep rebuilding</button>
          </>}
        </div>}
        {message && <p role="status">{message}</p>}{error && <p role="alert" className="reader-pane-error">Could not start records rebuild: {error} <button type="button" onClick={() => void rebuild()}>Retry</button></p>}
      </section>
    </section>
  );
}
