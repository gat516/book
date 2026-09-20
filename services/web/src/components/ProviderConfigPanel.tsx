import { hostedSession } from "../session";
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
import { ProviderHealth } from "./ProviderHealth";

interface Props {
  novelId: string;
  defaultOpen?: boolean;
}

const PROVIDERS: ProviderName[] = ["gemini", "groq", "deepseek", "anthropic", "ollama", "custom"];

// Per-novel provider settings for a novel that already exists. Previously this could only
// be set at creation time, which meant a novel whose provider turned out to be a bad fit
// had to be deleted and re-made -- taking its chapters with it.
export function ProviderConfigPanel({ novelId, defaultOpen = false }: Props) {
  const [current, setCurrent] = useState<ProviderConfigView | null>(null);
  const [loading, setLoading] = useState(true);
  const [provider, setProvider] = useState<ProviderName>("gemini");
  const [translationModel, setTranslationModel] = useState("");
  const [extractionModel, setExtractionModel] = useState("");
  // Blank means "same as the AI features model" (ingest-api defaults it the same way).
  const [factsModel, setFactsModel] = useState("");
  // A saved model that is not in the catalog must stay editable rather than being
  // silently rewritten to a listed one on the next save.
  const [custom, setCustom] = useState(false);
  const [extractionCustom, setExtractionCustom] = useState(false);
  const [baseURL, setBaseURL] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [availableModels, setAvailableModels] = useState<string[]>([]);
  // Providers with a key saved in Settings (migration 0035). Since 0080 that is the only
  // place a key can live, so this set is the whole answer to "can this book actually call
  // the provider it names" -- not a fallback behind a per-book key, as it once was.
  const [accountKeyProviders, setAccountKeyProviders] = useState<Set<string>>(new Set());
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
        setFactsModel(config.facts_model ?? "");
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
    // A failure here leaves the set empty, which reads as "no account key" and blocks the
    // save. That is the safe direction: saving a provider this install cannot authenticate
    // produces a book whose chapters fail one stage later, far from this panel.
    listProviderCredentials()
      .then((res) =>
        setAccountKeyProviders(
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
    setFactsModel("");
    setCustom(next === "custom");
    setExtractionCustom(next === "custom");
    setBaseURL("");
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
        facts_model: factsModel.trim() || undefined,
        base_url: baseURL.trim() || undefined,
      });
      setCurrent(view);
      setProvider(view.provider);
      setTranslationModel(view.translate_model ?? view.model ?? "");
      setExtractionModel(view.extract_model ?? view.model ?? "");
      setFactsModel(view.facts_model ?? "");
      setBaseURL(view.base_url ?? "");
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
  // Both derive from the provider SELECTED right now, not from the saved row. The old
  // panel keyed its key messaging off the saved row's api_key_set, so switching the
  // dropdown to another provider still reported the previous provider's key as this
  // book's -- while hiding the account key that would actually be used.
  const accountKey = accountKeyProviders.has(provider);
  const missingKey = needsKey && !accountKey;
  const incompleteCustom = provider === "custom" && (
    !baseURL.trim() || !translationModel.trim() || !extractionModel.trim()
  );
  const ollamaURLDirty =
    provider === "ollama" &&
    baseURL.trim() !== (current?.provider === "ollama" ? current.base_url ?? "" : "");

  return (
    <details className="reader-settings settings-section" id="provider-config" open={defaultOpen || undefined}>
      <summary>Translation and AI features</summary>
      {loading ? (
        <p>Loading…</p>
      ) : (
        <form onSubmit={submit}>
          {current ? (
            <div className="provider-current">
              <strong>Current configuration</strong>
              <dl>
                <div><dt>Provider</dt><dd>{PROVIDER_LABELS[current.provider]}</dd></div>
                <div><dt>Translation</dt><dd>{current.translate_model ?? current.model ?? "Server default"}</dd></div>
                <div><dt>AI features</dt><dd>{current.extract_model ?? current.model ?? "Server default"}</dd></div>
                <div><dt>Story facts</dt><dd>{current.facts_model ?? current.extract_model ?? current.model ?? "Server default"}</dd></div>
                {(current.provider === "ollama" || current.provider === "custom") && (
                  <div><dt>Endpoint</dt><dd>{current.base_url || "Book server default"}</dd></div>
                )}
              </dl>
            </div>
          ) : (
            <p>This book has no provider of its own and uses the server default.</p>
          )}
          <div className="provider-health-stack" aria-label="Provider health">
            <ProviderHealth novelId={novelId} track="translate" compact />
            <ProviderHealth novelId={novelId} track="extract" compact />
          </div>

          <label>
            Provider for this book{" "}
            <select value={provider} onChange={(e) => chooseProvider(e.target.value as ProviderName)}>
              {PROVIDERS.filter(p => !hostedSession() || p !== "ollama").map((name) => (
                <option key={name} value={name}>
                  {PROVIDER_LABELS[name]}
                </option>
              ))}
            </select>
          </label>

          <p className="settings-help">This provider handles both translation and AI features. You can choose a different model for each role below. Save its API key once in Account settings.</p>
          <fieldset className="provider-role"><legend>Translation</legend>
          <p className="settings-help">Produces the chapter text you read in the target language.</p>
          {provider !== "custom" && (
            <label>
              Translation model
              <select value={custom ? CUSTOM_MODEL : translationModel} onChange={(e) => chooseModel(e.target.value)}>
                {MODEL_OPTIONS[provider].map((option) => (
                  <option key={option.id} value={option.id}>{option.label}</option>
                ))}
                <option value={CUSTOM_MODEL}>Other…</option>
              </select>
            </label>
          )}
          {(custom || provider === "custom") && (
            <label>
              Translation model name
              <input value={translationModel} onChange={(e) => setTranslationModel(e.target.value)} placeholder="Exact model ID" required={provider === "custom"} />
            </label>
          )}
          {!custom && selectedNote && <p className="novel-create-form-hint">{selectedNote}</p>}
          </fieldset>
          <fieldset className="provider-role"><legend>AI features</legend>
          <p className="settings-help">Extracts characters and story knowledge, and answers Ask AI questions using chapters you have read.</p>
          {provider !== "custom" && (
            <label>
              AI features model
              <select value={extractionCustom ? CUSTOM_MODEL : extractionModel} onChange={(e) => chooseExtractionModel(e.target.value)}>
                {MODEL_OPTIONS[provider].map((option) => (
                  <option key={option.id} value={option.id}>{option.label}</option>
                ))}
                <option value={CUSTOM_MODEL}>Other…</option>
              </select>
            </label>
          )}
          {(extractionCustom || provider === "custom") && (
            <label>
              AI features model name
              <input value={extractionModel} onChange={(e) => setExtractionModel(e.target.value)} placeholder="Exact model ID" required={provider === "custom"} />
            </label>
          )}
            <label>
            Story facts model
            <input value={factsModel} onChange={(e) => setFactsModel(e.target.value)}
              placeholder="Same as the AI features model" />
          </label>
          <p className="settings-help">Writes each chapter's hidden story facts that wiki pages are built from.</p>
          </fieldset>
          <p className="settings-help">Optional semantic search helps Ask AI find chapter passages. Configure it separately in Account settings → Semantic search.</p>
          {MODEL_LIST_IS_ADVISORY[provider] && (
            <p className="novel-create-form-hint">
              Ollama serves whatever is pulled on the host, so this list is a hint — a model
              the machine doesn't have fails when a chapter runs, not when you save.
            </p>
          )}

          {(provider === "ollama" || provider === "custom") && (
            <label>
              {provider === "custom" ? "API base URL" : "Ollama URL"}
              <span className="novel-create-form-hint">
                {provider === "custom" ? "Include the API version path, such as /v1." : "Blank uses the book server's Ollama."}
              </span>
              <input
                type="url"
                value={baseURL}
                onChange={(e) => {
                  setBaseURL(e.target.value);
                  if (provider === "ollama") {
                    setAvailableModels([]);
                    setOllamaStatus(null);
                  }
                }}
                placeholder={provider === "custom" ? "https://models.example.com/v1" : "http://localhost:11434"}
                required={provider === "custom"}
              />
            </label>
          )}

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

          {/* One line about the key, always naming the SELECTED provider. There is no key
              box here: a book chooses a provider, Settings holds that provider's key. */}
          {needsKey && accountKey && (
            <p className="novel-create-form-hint">
              This book will use the {PROVIDER_LABELS[provider]} key from Account settings.
            </p>
          )}

          <button type="submit" className="btn-primary" disabled={pending || missingKey || incompleteCustom}>
            {pending ? "Saving…" : "Save provider"}
          </button>
          {missingKey && (
            <p className="novel-create-form-hint">
              No {PROVIDER_LABELS[provider]} key is saved. Add one under Account settings →
              Provider keys, then choose {PROVIDER_LABELS[provider]} here.
            </p>
          )}
          {saved && <p role="status">Saved. New chapters will use it; work already in flight finishes on the old one.</p>}
          {error && <p role="alert" className="chapter-list-error">{error}</p>}
        </form>
      )}
    </details>
  );
}
