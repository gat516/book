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
    <h2>Semantic search <span className="status-pill status-pill-quiet">Optional</span></h2>
    <p>Helps Ask AI find relevant passages by meaning. An embedding model creates a search index; it does not translate chapters or write answers.</p>
    <p className="settings-help">With search off, translation and AI features still work. Ask AI uses published story knowledge, with less access to chapter passages.</p>
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
            <option value="disabled">Off — use story knowledge only</option>
            <option value="gemini">Gemini</option>
            <option value="openrouter">OpenRouter</option>
            <option value="server">Use server settings</option>
          </select>
        </label>
        {config.provider === "auto" && <p className="settings-help">Uses the Gemini key saved above (or supplied by the server). Without a Gemini key, search stays off. No Ollama server is needed.</p>}
        {config.provider === "server" && <p className="settings-help">Follows the app operator’s embedding settings. New installations use Automatic; existing servers may use Ollama. Choose Automatic or a hosted provider here to avoid Ollama.</p>}
        {hosted && <>
          <label>Embedding model
            <input value={config.model} required disabled={pending} onChange={(e) => { setConfig({ ...config, model: e.target.value }); setNotice(""); }} />
          </label>
          <p className="settings-help">Choose an embedding model that supports 768-dimensional output. Chat and translation models cannot build this search index.</p>
          {!credentialsLoading && <p className="settings-help">{savedKey ? `Uses your saved ${config.provider === "gemini" ? "Gemini" : "OpenRouter"} key.` : `Add a ${config.provider === "gemini" ? "Gemini" : "OpenRouter"} key under Provider keys above, unless the server already supplies one. Search stays off without a key.`}</p>}
        </>}
        <p className="settings-help">Applies to all books. Changing providers or models starts a different search index for new chapters; older chapters remain readable and their story knowledge stays available.</p>
        <button type="submit" className="btn-primary" disabled={pending || (hosted && !config.model.trim())}>{pending ? "Saving…" : "Save search settings"}</button>
      </form>}
  </section>;
}
