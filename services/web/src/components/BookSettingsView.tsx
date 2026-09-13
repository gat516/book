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
        <button className="app-back" onClick={onClose}>← Back to book</button>
        <h1>{title ? `${title} settings` : "Book settings"}</h1>
        <p>Choose how this book is translated and how its knowledge is extracted.</p>
      </header>
      <ProviderConfigPanel key={`provider-${novelId}`} novelId={novelId} defaultOpen />
    </section>
  );
}
