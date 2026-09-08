import { useCallback, useEffect, useState } from "react";
import { getProviderConfig, listOllamaModels, listProviderCredentials, saveProviderConfig } from "../api";
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
  const [translationModel, setTranslationModel] = useState("");
  const [extractionModel, setExtractionModel] = useState("");
  // A saved model that is not in the catalog must stay editable rather than being
  // silently rewritten to a listed one on the next save.
  const [custom, setCustom] = useState(false);
  const [extractionCustom, setExtractionCustom] = useState(false);
  const [baseURL, setBaseURL] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [availableModels, setAvailableModels] = useState<string[]>([]);
  // Providers with an account-level key saved in Settings (migration 0035). A book with
  // no key of its own falls back to that one, so requiring a key here would be asking for
  // a secret the server already has.
  const [sharedKeyProviders, setSharedKeyProviders] = useState<Set<string>>(new Set());
	const [ollamaStatus, setOllamaStatus] = useState<"checking" | "connected" | "unreachable" | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const config = await getProviderConfig(novelId);
      setCurrent(config);
      if (config) {
        setProvider(config.provider);
      setTranslationModel(config.translate_model ?? config.model ?? "");
      setExtractionModel(config.extract_model ?? config.model ?? "");
        setCustom(
          !!(config.translate_model ?? config.model) && !MODEL_OPTIONS[config.provider].some((m) => m.id === (config.translate_model ?? config.model)),
        );
        setExtractionCustom(
          !!(config.extract_model ?? config.model) && !MODEL_OPTIONS[config.provider].some((m) => m.id === (config.extract_model ?? config.model)),
        );
        setBaseURL(config.base_url ?? "");
		if (config.provider === "ollama") {
			setOllamaStatus("checking");
			try {
				const models = await listOllamaModels(novelId);
				setAvailableModels(models);
				setOllamaStatus("connected");
			} catch {
				setOllamaStatus("unreachable");
			}
		} else {
			setOllamaStatus(null);
		}
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

  useEffect(() => {
    // Advisory only: a failure here just means the panel falls back to demanding a
    // per-novel key, which still works.
    listProviderCredentials()
      .then((res) =>
        setSharedKeyProviders(
          new Set(res.credentials.filter((c) => c.api_key_set).map((c) => c.provider)),
        ),
      )
      .catch(() => undefined);
  }, []);

  function chooseProvider(next: ProviderName) {
    setProvider(next);
    // Model ids do not carry across providers, so a stale one would just 404 at call
    // time. Always reset to the new provider's default.
    setTranslationModel(DEFAULT_MODEL[next] ?? "");
    setExtractionModel(DEFAULT_MODEL[next] ?? "");
    setCustom(false);
    setExtractionCustom(false);
  }

  function chooseModel(value: string) {
    if (value === CUSTOM_MODEL) {
      setCustom(true);
      setTranslationModel("");
    } else {
      setCustom(false);
      setTranslationModel(value);
    }
  }

  function chooseExtractionModel(value: string) {
    if (value === CUSTOM_MODEL) {
      setExtractionCustom(true);
      setExtractionModel("");
    } else {
      setExtractionCustom(false);
      setExtractionModel(value);
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
        translate_model: translationModel.trim() || undefined,
        extract_model: extractionModel.trim() || undefined,
        base_url: baseURL.trim() || undefined,
        // Omitted rather than sent empty: the server preserves the stored key when this is
        // absent, so an edit that only changes the model keeps working.
        api_key: apiKey.trim() || undefined,
      });
      setCurrent(view);
      setProvider(view.provider);
      setTranslationModel(view.translate_model ?? view.model ?? "");
      setExtractionModel(view.extract_model ?? view.model ?? "");
      setBaseURL(view.base_url ?? "");
      setApiKey("");
      setSaved(true);
      if (view.provider === "ollama") {
        // Model discovery is based on the persisted per-novel URL, not the draft input.
        // Reload only after PATCH completes so GET cannot race the save and query the
        // previous server while leaving a stale "0 models" result on screen.
        setAvailableModels([]);
        setOllamaStatus("checking");
        try {
          const models = await listOllamaModels(novelId);
          setAvailableModels(models);
          setOllamaStatus("connected");
        } catch (err) {
          setOllamaStatus("unreachable");
          setError(`Provider saved, but its Ollama server could not be queried: ${String(err)}`);
        }
      } else {
        setAvailableModels([]);
        setOllamaStatus(null);
      }
    } catch (err) {
      setError(String(err));
    } finally {
      setPending(false);
    }
  }

  async function loadOllamaModels() {
    setError(null);
	setAvailableModels([]);
		setOllamaStatus("checking");
    try {
      const models = await listOllamaModels(novelId);
      setAvailableModels(models);
		setOllamaStatus("connected");
    } catch (err) {
		setOllamaStatus("unreachable");
      setError(String(err));
    }
  }

  const needsKey = NEEDS_API_KEY[provider];
  const selectedNote = MODEL_OPTIONS[provider].find((m) => m.id === translationModel)?.note;
  const sharedKey = sharedKeyProviders.has(provider);
  const missingKey = needsKey && !current?.api_key_set && !sharedKey && !apiKey.trim();
  const ollamaURLDirty =
    provider === "ollama" &&
    baseURL.trim() !== (current?.provider === "ollama" ? current.base_url ?? "" : "");

  return (
    <details className="reader-settings" id="provider-config">
      <summary>Model provider</summary>
      {loading ? (
        <p>Loading…</p>
      ) : (
        <form onSubmit={submit}>
          <p>
            {current
              ? `This novel uses ${PROVIDER_LABELS[current.provider]}. Translation: ${current.translate_model ?? current.model ?? "server default"}; extraction: ${current.extract_model ?? current.model ?? "server default"}.${current.provider === "ollama" ? ` Endpoint: ${current.base_url || "Book server default"}.` : ""}`
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
            Translation model{" "}
            <select value={custom ? CUSTOM_MODEL : translationModel} onChange={(e) => chooseModel(e.target.value)}>
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
                value={translationModel}
                onChange={(e) => setTranslationModel(e.target.value)}
                placeholder="exact model id"
              />
            </label>
          )}
          {!custom && selectedNote && <p className="novel-create-form-hint">{selectedNote}</p>}
          <label>
            Extraction model{" "}
            <select value={extractionCustom ? CUSTOM_MODEL : extractionModel} onChange={(e) => chooseExtractionModel(e.target.value)}>
              {MODEL_OPTIONS[provider].map((option) => (
                <option key={option.id} value={option.id}>
                  {option.label}
                </option>
              ))}
              <option value={CUSTOM_MODEL}>Other…</option>
            </select>
          </label>
          {extractionCustom && (
            <label>
              Extraction model name{" "}
              <input
                value={extractionModel}
                onChange={(e) => setExtractionModel(e.target.value)}
                placeholder="exact model id"
              />
            </label>
          )}
          {MODEL_LIST_IS_ADVISORY[provider] && (
            <p className="novel-create-form-hint">
              Ollama serves whatever is pulled on the host, so this list is a hint — a model
              the machine doesn't have fails when a chapter runs, not when you save.
            </p>
          )}

          <label>
            Base URL{" "}
            {provider === "ollama" && (
              <span className="novel-create-form-hint">(blank = Book server's Ollama)</span>
            )}{" "}
            <input
              value={baseURL}
              onChange={(e) => {
                setBaseURL(e.target.value);
                if (provider === "ollama") {
                  // A result from the previously saved URL no longer describes the
                  // server shown in the input.
                  setAvailableModels([]);
                  setOllamaStatus(null);
                }
              }}
              placeholder="provider default"
            />
          </label>

          {provider === "ollama" && (
            <>
              <button
                type="button"
                disabled={pending || ollamaStatus === "checking" || ollamaURLDirty}
                onClick={() => void loadOllamaModels()}
              >
                Load models from saved Ollama URL
              </button>
              <p className="novel-create-form-hint">
                Saving automatically reloads the model list. The server queries the saved URL's fixed{" "}
                <code>/api/tags</code> endpoint; only hosts allowed by the server administrator can be queried.
              </p>
              {ollamaURLDirty && (
                <p className="novel-create-form-hint">Save provider before loading models from the edited URL.</p>
              )}
              {availableModels.length > 0 && (
                <p className="novel-create-form-hint">Available: {availableModels.join(", ")}</p>
              )}
			  {ollamaStatus === "checking" && <p className="novel-create-form-hint">Checking Ollama connection…</p>}
			  {ollamaStatus === "connected" && <p role="status" className="novel-create-form-hint">Ollama connected — {availableModels.length} model(s) available.</p>}
			  {ollamaStatus === "unreachable" && <p role="alert" className="chapter-list-error">Ollama server could not be reached. Check its URL, Tailscale connection, and server allowlist.</p>}
            </>
          )}

          {needsKey && (
            <label>
              API key{" "}
              <input
                type="password"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                autoComplete="off"
                placeholder={
                  current?.api_key_set
                    ? "leave blank to keep the saved key"
                    : sharedKey
                      ? "optional — the account key is used"
                      : "required"
                }
              />
            </label>
          )}

          {needsKey && !current?.api_key_set && sharedKey && (
            <p className="novel-create-form-hint">
              The {PROVIDER_LABELS[provider]} key saved in Settings is used for this novel.
              Enter one here only to bill this book to a different account.
            </p>
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
