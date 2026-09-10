import { useEffect, useState } from "react";
import { createNovel, listProviderCredentials } from "../api";
import type { ProviderName } from "../types";
import { CUSTOM_MODEL, DEFAULT_MODEL, MODEL_OPTIONS } from "../providers";

interface Props {
  onCreated: (novelId: string) => void;
  onCancel: () => void;
}

export function NovelCreateForm({ onCreated, onCancel }: Props) {
  const [title, setTitle] = useState("");
  const [sourceLang, setSourceLang] = useState("zh");
  const [targetLang, setTargetLang] = useState("en");
  const [genre, setGenre] = useState("");
  // "" = use the server's process-wide provider, i.e. no per-novel override at all.
  const [provider, setProvider] = useState("");
  const [model, setModel] = useState("");
  const [customModel, setCustomModel] = useState(false);
  const [baseURL, setBaseURL] = useState("");
  const [ingestLookahead, setIngestLookahead] = useState("50");
  const [translateLookahead, setTranslateLookahead] = useState("5");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Providers with a key saved in Settings (migration 0035). Since 0080 that is the only
  // place a key lives, so this decides whether the chosen provider can be used at all.
  const [accountKeyProviders, setAccountKeyProviders] = useState<Set<string>>(new Set());

  useEffect(() => {
    // Advisory: if this fails the form shows no key note. Creation still succeeds -- an
    // unusable provider is fixed by adding the key in Settings, not by re-creating the book.
    listProviderCredentials()
      .then((res) =>
        setAccountKeyProviders(
          new Set(res.credentials.filter((c) => c.api_key_set).map((c) => c.provider)),
        ),
      )
      .catch(() => undefined);
  }, []);

  function chooseProvider(next: string) {
    setProvider(next);
    setModel(DEFAULT_MODEL[next] ?? "");
    setCustomModel(false);
  }

  function chooseModel(value: string) {
    setCustomModel(value === CUSTOM_MODEL);
    setModel(value === CUSTOM_MODEL ? "" : value);
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!title.trim()) return;
    setPending(true);
    setError(null);
    try {
      const response = await createNovel({
        title,
        source_lang: sourceLang || undefined,
        target_lang: targetLang || undefined,
        genre: genre || undefined,
        provider_config: provider
          ? {
              provider: provider as "anthropic" | "deepseek" | "gemini" | "groq" | "ollama",
              model: model.trim() || undefined,
              base_url: baseURL.trim() || undefined,
            }
          : undefined,
        // Number("") is 0, which legitimately means "unlimited" — so an empty box has to
        // mean "server default" (undefined) rather than silently becoming unlimited.
        ingest_lookahead: ingestLookahead === "" ? undefined : Number(ingestLookahead),
        translate_lookahead: translateLookahead === "" ? undefined : Number(translateLookahead),
      });
      onCreated(response.id);
    } catch (err) {
      setError(String(err));
    } finally {
      setPending(false);
    }
  }

  return (
    <form className="novel-create-form" onSubmit={submit}>
      <h1>New novel</h1>
      <label>
        Title
        <input value={title} onChange={(e) => setTitle(e.target.value)} required />
      </label>
      <label>
        Source language
        <input value={sourceLang} onChange={(e) => setSourceLang(e.target.value)} placeholder="zh" />
      </label>
      <label>
        Target language
        <input value={targetLang} onChange={(e) => setTargetLang(e.target.value)} placeholder="en" />
      </label>
      <label>
        Genre <span className="novel-create-form-hint">(optional — selects a preset ontology)</span>
        <input value={genre} onChange={(e) => setGenre(e.target.value)} placeholder="xianxia" />
      </label>
      <fieldset>
        <legend>Translation model</legend>
        <label>
          Provider{" "}
          <span className="novel-create-form-hint">(leave as default to use the server's)</span>
          <select value={provider} onChange={(e) => chooseProvider(e.target.value)}>
            <option value="">Server default</option>
            <option value="deepseek">DeepSeek</option>
            <option value="anthropic">Anthropic</option>
            <option value="gemini">Gemini</option>
            <option value="groq">Groq</option>
            <option value="ollama">Ollama (local)</option>
          </select>
        </label>
        {provider && (
          <>
            <label>
              Model
              <select
                value={customModel ? CUSTOM_MODEL : model}
                onChange={(e) => chooseModel(e.target.value)}
              >
                {(MODEL_OPTIONS[provider as ProviderName] ?? []).map((option) => (
                  <option key={option.id} value={option.id}>
                    {option.label}
                  </option>
                ))}
                <option value={CUSTOM_MODEL}>Other…</option>
              </select>
            </label>
            {customModel && (
              <label>
                Model name
                <input
                  value={model}
                  onChange={(e) => setModel(e.target.value)}
                  placeholder="exact model id"
                />
              </label>
            )}
            {provider !== "ollama" ? (
              accountKeyProviders.has(provider) ? (
                <p className="novel-create-form-hint">
                  This book will use the {provider} key from Account settings.
                </p>
              ) : (
                <p className="novel-create-form-hint">
                  No {provider} key is saved yet. The book can still be created — add the key
                  under Account settings → Provider keys before its first chapter runs.
                </p>
              )
            ) : (
              <label>
                Host <span className="novel-create-form-hint">(blank = server's OLLAMA_HOST)</span>
                <input
                  value={baseURL}
                  onChange={(e) => setBaseURL(e.target.value)}
                  placeholder="http://localhost:11434"
                />
              </label>
            )}
          </>
        )}
      </fieldset>

      <fieldset>
        <legend>How far ahead to work</legend>
        <label>
          Fetch ahead{" "}
          <span className="novel-create-form-hint">chapters to download past where you are</span>
          <input
            type="number"
            min={0}
            value={ingestLookahead}
            onChange={(e) => setIngestLookahead(e.target.value)}
          />
        </label>
        <label>
          Translate ahead{" "}
          <span className="novel-create-form-hint">
            chapters to translate past where you are — far costlier than fetching
          </span>
          <input
            type="number"
            min={0}
            value={translateLookahead}
            onChange={(e) => setTranslateLookahead(e.target.value)}
          />
        </label>
        <p className="novel-create-form-hint">0 means unlimited. Both can be changed later.</p>
      </fieldset>

      <div className="novel-create-form-actions">
        <button type="button" onClick={onCancel} disabled={pending}>
          Cancel
        </button>
        <button type="submit" disabled={pending || !title.trim()}>
          {pending ? "Creating…" : "Create"}
        </button>
      </div>
      {error && <p className="novel-create-form-error">{error}</p>}
    </form>
  );
}
