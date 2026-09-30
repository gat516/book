import { ArrowLeft, BookOpen, KeyRound, Save, Trash2 } from "lucide-react";
import { Button } from "./animate-ui/motion";
import { SemanticSearchSettings } from "./SemanticSearchSettings";
import { useCallback, useEffect, useState } from "react";
import { deleteProviderCredential, listProviderCredentials, saveProviderCredential } from "../api";
import { PROVIDER_LABELS } from "../providers";
import type { ProviderCredentialView, CredentialProviderName } from "../types";

interface Props {
  clickableEntities: boolean;
  onChangeClickableEntities: (enabled: boolean) => void;
  onClose: () => void;
}

const PROVIDERS: CredentialProviderName[] = ["gemini", "groq", "deepseek", "anthropic", "custom", "openrouter"];
const LABELS = { ...PROVIDER_LABELS, openrouter: "OpenRouter" };

// Account-wide settings: the provider keys every book draws on, plus reading preferences.
// Keys live here rather than per book (migration 0035) because the common case is several
// books on one account -- re-pasting the same secret per book had no way to rotate it in
// one place. A book still chooses its own provider and model.
export function SettingsView({ clickableEntities, onChangeClickableEntities, onClose }: Props) {
  const [credentials, setCredentials] = useState<ProviderCredentialView[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Record<string, { apiKey: string; baseURL?: string }>>({});
  const [pending, setPending] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await listProviderCredentials();
      setCredentials(response.credentials);
    } catch (err) {
      setError(String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  function draft(provider: string) {
    return drafts[provider] ?? { apiKey: "" };
  }

  function setDraft(provider: string, patch: Partial<{ apiKey: string; baseURL?: string }>) {
    setDrafts((d) => ({ ...d, [provider]: { ...draft(provider), ...patch } }));
  }

  async function save(provider: CredentialProviderName) {
    setPending(provider);
    setError(null);
    setNotice(null);
    try {
      const current = draft(provider);
      const response = await saveProviderCredential(provider, {
        api_key: current.apiKey.trim() || undefined,
        base_url: current.baseURL?.trim() || undefined,
      });
      setCredentials(response.credentials);
      setDraft(provider, { apiKey: "" });
      setNotice(`Saved ${LABELS[provider]}.`);
    } catch (err) {
      setError(String(err));
    } finally {
      setPending(null);
    }
  }

  async function remove(provider: CredentialProviderName) {
    setPending(provider);
    setError(null);
    setNotice(null);
    try {
      await deleteProviderCredential(provider);
      await load();
      setNotice(`Removed the ${LABELS[provider]} key. Books using this provider need another key or a provider change.`);
    } catch (err) {
      setError(String(err));
    } finally {
      setPending(null);
    }
  }

  const saved = new Map(credentials.map((c) => [c.provider, c]));

  return (
    <section className="settings-view">
      <header className="settings-page-header">
        <button className="app-back" onClick={onClose}><ArrowLeft size={16} />Back</button>
        <h1>Account settings</h1>
      </header>
      {error && <p role="alert" className="chapter-list-error">{error}</p>}
      {notice && <p role="status" className="settings-notice">{notice}</p>}

      <section className="settings-section">
        <h2><BookOpen size={19} />Reading</h2>
        <label className="settings-toggle">
          <span>
            <strong>Show hover previews</strong>
          </span>
          <input
            type="checkbox"
            checked={!clickableEntities}
            onChange={(event) => onChangeClickableEntities(!event.target.checked)}
          />
        </label>
        <p className="settings-help">
          Names are always clickable. Saved in this browser.
        </p>
      </section>

      <section className="settings-section">
        <div className="settings-section-heading">
          <div>
            <h2><KeyRound size={19} />Provider keys</h2>
            <p>Shared across your books. Usage is billed by your provider.</p>
          </div>
        </div>
        {loading ? (
          <p>Loading providers…</p>
        ) : (
          <div className="settings-provider-list">
            {PROVIDERS.map((provider) => {
              const current = saved.get(provider);
              const busy = pending === provider;
              return (
                <details key={provider} className="settings-provider">
                  <summary>
                    <span>{LABELS[provider]}<small className="provider-purpose">{provider === "openrouter" ? "Semantic search only" : provider === "gemini" ? "Translation, AI features, and semantic search" : "Translation and AI features"}</small></span>
                    <span className={`status-pill ${current?.api_key_set ? "status-pill-live" : "status-pill-quiet"}`}>
                      {current?.api_key_set ? "Key saved" : "No key"}
                    </span>
                  </summary>
                  <div className="settings-provider-content">
                    <label>
                      API key
                      <input
                        type="password"
                        autoComplete="off"
                        value={draft(provider).apiKey}
                        onChange={(e) => setDraft(provider, { apiKey: e.target.value })}
                        placeholder={current?.api_key_set ? "Leave blank to keep the saved key" : "Paste a key"}
                      />
                    </label>
                    {provider === "custom" && (<><label>Approved API endpoint<input type="url" value={draft(provider).baseURL ?? ""} onChange={e => setDraft(provider, {baseURL: e.target.value})} placeholder="https://models.example.com/v1" /></label></>)}
                    {provider === "custom" && (
                      <p className="settings-help">Set the OpenAI-compatible endpoint separately in each book's settings.</p>
                    )}
                    <div className="settings-actions">
                      {current?.api_key_set && (
                        <button className="btn-danger" onClick={() => remove(provider)} disabled={busy}>
                          <Trash2 size={15} />Remove key
                        </button>
                      )}
                      <Button className="btn-primary" onClick={() => save(provider)} disabled={busy}>
                        <Save size={15} />{busy ? "Saving…" : "Save provider"}
                      </Button>
                    </div>
                  </div>
                </details>
              );
            })}
          </div>
        )}
        <p className="settings-help">
          Keys are encrypted and cannot be viewed after saving.
        </p>
      </section>
      <SemanticSearchSettings credentials={credentials} credentialsLoading={loading} />
    </section>
  );
}
