import { useState } from "react";
import { createNovel } from "../api";

interface Props {
  onCreated: (novelId: string) => void;
  onCancel: () => void;
}

// Sensible per-provider defaults so choosing a provider doesn't also require knowing its
// model names. Empty means "let the server decide".
const DEFAULT_MODEL: Record<string, string> = {
  deepseek: "deepseek-chat",
  anthropic: "claude-haiku-4-5",
  gemini: "gemini-3.5-flash",
  ollama: "",
};

export function NovelCreateForm({ onCreated, onCancel }: Props) {
  const [title, setTitle] = useState("");
  const [sourceLang, setSourceLang] = useState("zh");
  const [targetLang, setTargetLang] = useState("en");
  const [genre, setGenre] = useState("");
  // "" = use the server's process-wide provider, i.e. no per-novel override at all.
  const [provider, setProvider] = useState("");
  const [model, setModel] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [baseURL, setBaseURL] = useState("");
  const [ingestLookahead, setIngestLookahead] = useState("50");
  const [translateLookahead, setTranslateLookahead] = useState("5");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function chooseProvider(next: string) {
    setProvider(next);
    setModel(DEFAULT_MODEL[next] ?? "");
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
              provider: provider as "anthropic" | "deepseek" | "gemini" | "ollama",
              model: model.trim() || undefined,
              base_url: baseURL.trim() || undefined,
              api_key: apiKey.trim() || undefined,
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
            <option value="ollama">Ollama (local)</option>
          </select>
        </label>
        {provider && (
          <>
            <label>
              Model
              <input
                value={model}
                onChange={(e) => setModel(e.target.value)}
                placeholder={DEFAULT_MODEL[provider] || "server default"}
              />
            </label>
            {provider !== "ollama" ? (
              <label>
                API key{" "}
                <span className="novel-create-form-hint">
                  (encrypted before storage; never shown again)
                </span>
                <input
                  type="password"
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                  autoComplete="off"
                />
              </label>
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
