import { ProviderConfigPanel } from "./ProviderConfigPanel";

interface Props {
  novelId: string;
  onClose: () => void;
}

// Per-book settings: provider/model configuration and knowledge repair. Both used to sit
// permanently at the top of the reader, polling and rendering on every page even while
// someone was just reading -- this is book-level, same as the reader's "← All chapters"
// boundary, not the account-level SettingsView.
export function BookSettingsView({ novelId, onClose }: Props) {
  return (
    <section className="settings-view">
      <button className="app-back" onClick={onClose}>
        ← Back
      </button>
      <h2>Book settings</h2>

      <ProviderConfigPanel key={`provider-${novelId}`} novelId={novelId} />
    </section>
  );
}
