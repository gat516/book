import { Search, Save } from "lucide-react";
import { useEffect, useState } from "react";
import { getEmbeddingConfig, saveEmbeddingConfig } from "../api";
import type { EmbeddingConfig, ProviderCredentialView } from "../types";

export function SemanticSearchSettings({ credentials, credentialsLoading }: {
  credentials: ProviderCredentialView[]; credentialsLoading: boolean;
}) {
  const [config, setConfig] = useState<EmbeddingConfig | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  useEffect(() => { getEmbeddingConfig().then(setConfig).catch((err) => setError(String(err))); }, []);
  const hosted = config?.provider === "gemini" || config?.provider === "openrouter";
  const savedKey = credentials.some((c) => c.provider === config?.provider && c.api_key_set);

  async function save(event: React.FormEvent) {
    event.preventDefault();
    if (!config) return;
    setPending(true); setError(""); setNotice("");
    try {
      setConfig(await saveEmbeddingConfig(config));
      setNotice("Saved. Applies to new chapters and the next Ask AI question. Existing chapters are not re-indexed automatically.");
    } catch (err) { setError(String(err)); }
    finally { setPending(false); }
  }

  return <section className="settings-section">
    <h2><Search size={19} />Semantic search <span className="status-pill status-pill-quiet">Optional</span></h2>
    <p>Find relevant chapter passages for Ask AI.</p>
    {error && <p role="alert" className="chapter-list-error">{error}</p>}
    {notice && <p role="status" className="settings-notice">{notice}</p>}
    {!config ? <p>{error ? "Settings could not be loaded. Reopen this page to retry." : "Loading semantic search…"}</p> :
      <form onSubmit={save} className="semantic-search-form">
        <label>Search provider
          <select value={config.provider} disabled={pending} onChange={(e) => {
            const provider = e.target.value as EmbeddingConfig["provider"];
            setConfig({ provider, model: provider === "gemini" ? "gemini-embedding-001" : provider === "openrouter" ? "openai/text-embedding-3-small" : "" });
            setNotice("");
          }}>
            <option value="auto">Automatic — use Gemini when a key is available</option>
            <option value="disabled">Off</option>
            <option value="gemini">Gemini</option>
            <option value="openrouter">OpenRouter</option>
            <option value="server">Use server settings</option>
          </select>
        </label>
        {config.provider === "auto" && <p className="settings-help">Uses an available Gemini key. Search stays off without one.</p>}
        {config.provider === "server" && <p className="settings-help">Uses the server’s search configuration.</p>}
        {hosted && <>
          <label>Embedding model
            <input value={config.model} required disabled={pending} onChange={(e) => { setConfig({ ...config, model: e.target.value }); setNotice(""); }} />
          </label>
          <p className="settings-help">Requires an embedding model with 768-dimensional output.</p>
          {!credentialsLoading && <p className="settings-help">{savedKey ? `Uses your saved ${config.provider === "gemini" ? "Gemini" : "OpenRouter"} key.` : `Add a ${config.provider === "gemini" ? "Gemini" : "OpenRouter"} key above. Search stays off without an available key.`}</p>}
        </>}
        <p className="settings-help">Applies to all books. Provider or model changes affect new chapters; existing chapters are not re-indexed.</p>
        <button type="submit" className="btn-primary" disabled={pending || (hosted && !config.model.trim())}><Save size={15} />{pending ? "Saving…" : "Save search settings"}</button>
      </form>}
  </section>;
}
