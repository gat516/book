import { useCallback, useEffect, useState } from "react";
import { deleteProviderCredential, listProviderCredentials, saveProviderCredential } from "../api";
import { NEEDS_API_KEY, PROVIDER_LABELS } from "../providers";
import type { ProviderCredentialView, ProviderName } from "../types";

interface Props {
  clickableEntities: boolean;
  onChangeClickableEntities: (enabled: boolean) => void;
  onClose: () => void;
}

const PROVIDERS: ProviderName[] = ["gemini", "deepseek", "anthropic", "ollama"];

// Account-wide settings: the provider keys every book draws on, plus reading preferences.
// Keys live here rather than per book (migration 0035) because the common case is several
// books on one account -- re-pasting the same secret per book had no way to rotate it in
// one place. A book still chooses its own provider and model.
export function SettingsView({ clickableEntities, onChangeClickableEntities, onClose }: Props) {
  const [credentials, setCredentials] = useState<ProviderCredentialView[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Record<string, { apiKey: string; baseURL: string }>>({});
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
    return drafts[provider] ?? { apiKey: "", baseURL: "" };
  }

  function setDraft(provider: string, patch: Partial<{ apiKey: string; baseURL: string }>) {
    setDrafts((d) => ({ ...d, [provider]: { ...draft(provider), ...patch } }));
  }

  async function save(provider: ProviderName) {
    setPending(provider);
    setError(null);
    setNotice(null);
    try {
      const current = draft(provider);
      const response = await saveProviderCredential(provider, {
        // Omitted rather than empty: the server keeps the stored key when this is absent,
        // so editing a base URL alone cannot wipe the secret.
        api_key: current.apiKey.trim() || undefined,
        base_url: current.baseURL.trim() || undefined,
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
      <button className="app-back" onClick={onClose}>
        ← Back
      </button>
      <h2>Settings</h2>

      <h3>Provider keys</h3>
      <p>
        Entered once and shared by every book. Each book then picks which provider and model
        to use in its own “Model provider” panel.
      </p>
      {error && <p role="alert" className="chapter-list-error">{error}</p>}
      {notice && <p role="status">{notice}</p>}

      {loading ? (
        <p>Loading…</p>
      ) : (
        PROVIDERS.filter((p) => NEEDS_API_KEY[p]).map((provider) => {
          const current = saved.get(provider);
          const busy = pending === provider;
          return (
            <div key={provider} className="settings-provider">
              <h4>
                {PROVIDER_LABELS[provider]}{" "}
                {current?.api_key_set ? <span>— key saved</span> : <span>— no key</span>}
              </h4>
              <label>
                API key{" "}
                <input
                  type="password"
                  autoComplete="off"
                  value={draft(provider).apiKey}
                  onChange={(e) => setDraft(provider, { apiKey: e.target.value })}
                  placeholder={current?.api_key_set ? "leave blank to keep the saved key" : "paste a key"}
                />
              </label>
              <label>
                Base URL{" "}
                <input
                  value={draft(provider).baseURL || current?.base_url || ""}
                  onChange={(e) => setDraft(provider, { baseURL: e.target.value })}
                  placeholder="provider default"
                />
              </label>
              <button onClick={() => save(provider)} disabled={busy}>
                {busy ? "Saving…" : "Save"}
              </button>
              {current?.api_key_set && (
                <button onClick={() => remove(provider)} disabled={busy}>
                  Remove key
                </button>
              )}
            </div>
          );
        })
      )}
      <p className="novel-create-form-hint">
        Keys are encrypted before they are stored and are never sent back to this page —
        it only ever learns whether one exists.
      </p>

      <h3>Reading</h3>
      <label>
        <input
          type="checkbox"
          checked={!clickableEntities}
          onChange={(event) => onChangeClickableEntities(!event.target.checked)}
        />{" "}
        Show hover previews
      </label>
      <p>
        Highlighted names are always clickable, even when no information is linked yet.
        Enable previews to also see a card on hover. Saved in this browser, so it does not
        follow you to another device.
      </p>
    </section>
  );
}
