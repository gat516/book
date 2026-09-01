import { useCallback, useEffect, useState } from "react";
import { getProviderConfig, saveProviderConfig } from "../api";
import {
  CUSTOM_MODEL,
  DEFAULT_MODEL,
  MODEL_LIST_IS_ADVISORY,
  MODEL_OPTIONS,
  NEEDS_API_KEY,
  PROVIDER_LABELS,
} from "../providers";
import type { ProviderConfigView, ProviderName } from "../types";

interface Props {
  novelId: string;
}

const PROVIDERS: ProviderName[] = ["gemini", "deepseek", "anthropic", "ollama"];

// Per-novel provider settings for a novel that already exists. Previously this could only
// be set at creation time, which meant a novel whose provider turned out to be a bad fit
// had to be deleted and re-made -- taking its chapters with it.
export function ProviderConfigPanel({ novelId }: Props) {
  const [current, setCurrent] = useState<ProviderConfigView | null>(null);
  const [loading, setLoading] = useState(true);
  const [provider, setProvider] = useState<ProviderName>("gemini");
  const [model, setModel] = useState("");
  // A saved model that is not in the catalog must stay editable rather than being
  // silently rewritten to a listed one on the next save.
  const [custom, setCustom] = useState(false);
  const [baseURL, setBaseURL] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const config = await getProviderConfig(novelId);
      setCurrent(config);
      if (config) {
        setProvider(config.provider);
        setModel(config.model ?? "");
        setCustom(
          !!config.model && !MODEL_OPTIONS[config.provider].some((m) => m.id === config.model),
        );
        setBaseURL(config.base_url ?? "");
      }
    } catch (err) {
      setError(String(err));
    } finally {
      setLoading(false);
    }
  }, [novelId]);

  useEffect(() => {
    void load();
  }, [load]);

  function chooseProvider(next: ProviderName) {
    setProvider(next);
    // Model ids do not carry across providers, so a stale one would just 404 at call
    // time. Always reset to the new provider's default.
    setModel(DEFAULT_MODEL[next] ?? "");
    setCustom(false);
  }

  function chooseModel(value: string) {
    if (value === CUSTOM_MODEL) {
      setCustom(true);
      setModel("");
    } else {
      setCustom(false);
      setModel(value);
    }
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setPending(true);
    setError(null);
    setSaved(false);
    try {
      const view = await saveProviderConfig(novelId, {
        provider,
        model: model.trim() || undefined,
        base_url: baseURL.trim() || undefined,
        // Omitted rather than sent empty: the server preserves the stored key when this is
        // absent, so an edit that only changes the model keeps working.
        api_key: apiKey.trim() || undefined,
      });
      setCurrent(view);
      setApiKey("");
      setSaved(true);
    } catch (err) {
      setError(String(err));
    } finally {
      setPending(false);
    }
  }

  const needsKey = NEEDS_API_KEY[provider];
  const selectedNote = MODEL_OPTIONS[provider].find((m) => m.id === model)?.note;
  const missingKey = needsKey && !current?.api_key_set && !apiKey.trim();

  return (
    <details className="reader-settings">
      <summary>Model provider</summary>
      {loading ? (
        <p>Loading…</p>
      ) : (
        <form onSubmit={submit}>
          <p>
            {current
              ? `This novel uses ${PROVIDER_LABELS[current.provider]}${current.model ? ` (${current.model})` : ""}.`
              : "This novel has no provider of its own and uses the server default."}
          </p>

          <label>
            Provider{" "}
            <select value={provider} onChange={(e) => chooseProvider(e.target.value as ProviderName)}>
              {PROVIDERS.map((name) => (
                <option key={name} value={name}>
                  {PROVIDER_LABELS[name]}
                </option>
              ))}
            </select>
          </label>

          <label>
            Model{" "}
            <select value={custom ? CUSTOM_MODEL : model} onChange={(e) => chooseModel(e.target.value)}>
              {MODEL_OPTIONS[provider].map((option) => (
                <option key={option.id} value={option.id}>
                  {option.label}
                </option>
              ))}
              <option value={CUSTOM_MODEL}>Other…</option>
            </select>
          </label>
          {custom && (
            <label>
              Model name{" "}
              <input
                value={model}
                onChange={(e) => setModel(e.target.value)}
                placeholder="exact model id"
              />
            </label>
          )}
          {!custom && selectedNote && <p className="novel-create-form-hint">{selectedNote}</p>}
          {MODEL_LIST_IS_ADVISORY[provider] && (
            <p className="novel-create-form-hint">
              Ollama serves whatever is pulled on the host, so this list is a hint — a model
              the machine doesn't have fails when a chapter runs, not when you save.
            </p>
          )}

          <label>
            Base URL{" "}
            <input
              value={baseURL}
              onChange={(e) => setBaseURL(e.target.value)}
              placeholder="provider default"
            />
          </label>

          {needsKey && (
            <label>
              API key{" "}
              <input
                type="password"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                autoComplete="off"
                placeholder={current?.api_key_set ? "leave blank to keep the saved key" : "required"}
              />
            </label>
          )}

          {needsKey && current?.api_key_set && (
            <p className="novel-create-form-hint">
              A key is saved for this novel. It is encrypted at rest and never shown again —
              leave the box blank to keep it, or type a new one to replace it.
            </p>
          )}

          <button type="submit" disabled={pending || missingKey}>
            {pending ? "Saving…" : "Save provider"}
          </button>
          {missingKey && (
            <p className="novel-create-form-hint">
              {PROVIDER_LABELS[provider]} needs an API key before it can be saved.
            </p>
          )}
          {saved && <p role="status">Saved. New chapters will use it; work already in flight finishes on the old one.</p>}
          {error && <p role="alert" className="chapter-list-error">{error}</p>}
        </form>
      )}
    </details>
  );
}
