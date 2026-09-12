import { ProviderConfigPanel } from "./ProviderConfigPanel";
import { rebuildRecords } from "../api";
import { useState } from "react";

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
  const [busy, setBusy] = useState(false);
  async function rebuild() {
    setBusy(true); setMessage(null);
    try { const result = await rebuildRecords(novelId); setMessage(`Records rebuild started (${result.generation_id}).`); }
    catch (error) { setMessage(String(error)); }
    finally { setBusy(false); }
  }
  return (
    <section className="settings-view">
      <button className="app-back" onClick={onClose}>
        ← Back
      </button>
      <h2>Book settings</h2>

      <ProviderConfigPanel key={`provider-${novelId}`} novelId={novelId} />
      <section className="records-settings"><h3>Knowledge records</h3><p>Rebuild extraction in a new immutable generation when the ontology or extraction configuration changes.</p><button type="button" onClick={() => void rebuild()} disabled={busy}>{busy ? "Starting rebuild…" : "Rebuild records"}</button>{message && <p role="status">{message}</p>}</section>
    </section>
  );
}
