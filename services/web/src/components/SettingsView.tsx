import { useCallback, useEffect, useState } from "react";
import { deleteProviderCredential, listProviderCredentials, saveProviderCredential } from "../api";
import { NEEDS_API_KEY, PROVIDER_LABELS } from "../providers";
import type { ProviderCredentialView, ProviderName } from "../types";

interface Props {
  clickableEntities: boolean;
  onChangeClickableEntities: (enabled: boolean) => void;
  onClose: () => void;
}

const PROVIDERS: ProviderName[] = ["gemini", "groq", "deepseek", "anthropic", "custom", "ollama"];

// Account-wide settings: the provider keys every book draws on, plus reading preferences.
// Keys live here rather than per book (migration 0035) because the common case is several
// books on one account -- re-pasting the same secret per book had no way to rotate it in
// one place. A book still chooses its own provider and model.
export function SettingsView({ clickableEntities, onChangeClickableEntities, onClose }: Props) {
  const [credentials, setCredentials] = useState<ProviderCredentialView[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Record<string, { apiKey: string }>>({});
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

  function setDraft(provider: string, patch: Partial<{ apiKey: string }>) {
    setDrafts((d) => ({ ...d, [provider]: { ...draft(provider), ...patch } }));
  }

  async function save(provider: ProviderName) {
    setPending(provider);
    setError(null);
    setNotice(null);
    try {
      const current = draft(provider);
      const response = await saveProviderCredential(provider, {
        api_key: current.apiKey.trim() || undefined,
      });
      setCredentials(response.credentials);
      setDraft(provider, { apiKey: "" });
      setNotice(`Saved ${PROVIDER_LABELS[provider]}.`);
    } catch (err) {
      setError(String(err));
    } finally {
      setPending(null);
    }
  }

  async function remove(provider: ProviderName) {
    setPending(provider);
    setError(null);
    setNotice(null);
    try {
      await deleteProviderCredential(provider);
      await load();
      setNotice(`Removed the ${PROVIDER_LABELS[provider]} key. Books using it fall back to the server default.`);
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
        <button className="app-back" onClick={onClose}>← Back</button>
        <h1>Account settings</h1>
        <p>Preferences and provider credentials shared by every book.</p>
      </header>
      {error && <p role="alert" className="chapter-list-error">{error}</p>}
      {notice && <p role="status" className="settings-notice">{notice}</p>}

      <section className="settings-section">
        <h2>Reading</h2>
        <label className="settings-toggle">
          <span>
            <strong>Show hover previews</strong>
            <small>Preview linked names when the pointer rests over them.</small>
          </span>
          <input
            type="checkbox"
            checked={!clickableEntities}
            onChange={(event) => onChangeClickableEntities(!event.target.checked)}
          />
        </label>
        <p className="settings-help">
          Highlighted names remain clickable either way. This preference is saved only in this browser.
        </p>
      </section>

      <section className="settings-section">
        <div className="settings-section-heading">
          <div>
            <h2>Provider keys</h2>
            <p>Saved once, then available to every book that selects that provider.</p>
          </div>
        </div>
        {loading ? (
          <p>Loading providers…</p>
        ) : (
          <div className="settings-provider-list">
            {PROVIDERS.filter((p) => NEEDS_API_KEY[p]).map((provider) => {
              const current = saved.get(provider);
              const busy = pending === provider;
              return (
                <details key={provider} className="settings-provider">
                  <summary>
                    <span>{PROVIDER_LABELS[provider]}</span>
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
                    {provider === "custom" && (
                      <p className="settings-help">Set the OpenAI-compatible endpoint separately in each book's settings.</p>
                    )}
                    <div className="settings-actions">
                      {current?.api_key_set && (
                        <button className="btn-danger" onClick={() => remove(provider)} disabled={busy}>
                          Remove key
                        </button>
                      )}
                      <button className="btn-primary" onClick={() => save(provider)} disabled={busy}>
                        {busy ? "Saving…" : "Save provider"}
                      </button>
                    </div>
                  </div>
                </details>
              );
            })}
          </div>
        )}
        <p className="settings-help">
          Keys are encrypted before storage and never sent back to this page; it only learns whether one exists.
        </p>
      </section>
    </section>
  );
}
