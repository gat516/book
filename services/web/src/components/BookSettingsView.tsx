import { ArrowLeft } from "lucide-react";
import { ProviderConfigPanel } from "./ProviderConfigPanel";

interface Props {
  novelId: string;
  title?: string;
  onClose: () => void;
}

// Per-book settings: provider/model configuration and knowledge repair. Both used to sit
// permanently at the top of the reader, polling and rendering on every page even while
// someone was just reading -- this is book-level, same as the reader's "← All chapters"
// boundary, not the account-level SettingsView.
export function BookSettingsView({ novelId, title, onClose }: Props) {
  return (
    <section className="settings-view">
      <header className="settings-page-header">
        <button className="app-back" onClick={onClose}><ArrowLeft size={16} />Back to book</button>
        <h1>{title ? `${title} settings` : "Book settings"}</h1>
      </header>
      <ProviderConfigPanel key={`provider-${novelId}`} novelId={novelId} defaultOpen />
    </section>
  );
}
