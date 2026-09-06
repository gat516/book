import { ProviderConfigPanel } from "./ProviderConfigPanel";
import { RepairPanel } from "./RepairPanel";

interface Props {
  novelId: string;
  // Forwarded to RepairPanel: incremented when something elsewhere (the reader's "facts
  // are withheld" notice) wants the repair panel opened, not just this page shown.
  repairOpenSignal: number;
  onClose: () => void;
}

// Per-book settings: provider/model configuration and knowledge repair. Both used to sit
// permanently at the top of the reader, polling and rendering on every page even while
// someone was just reading -- this is book-level, same as the reader's "← All chapters"
// boundary, not the account-level SettingsView.
export function BookSettingsView({ novelId, repairOpenSignal, onClose }: Props) {
  return (
    <section className="settings-view">
      <button className="app-back" onClick={onClose}>
        ← Back
      </button>
      <h2>Book settings</h2>

      <ProviderConfigPanel key={`provider-${novelId}`} novelId={novelId} />
      <RepairPanel key={`repair-${novelId}`} novelId={novelId} openSignal={repairOpenSignal} />
    </section>
  );
}
