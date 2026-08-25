import { useState } from "react";
import { createNovel } from "../api";

interface Props {
  onCreated: (novelId: string) => void;
  onCancel: () => void;
}

export function NovelCreateForm({ onCreated, onCancel }: Props) {
  const [title, setTitle] = useState("");
  const [sourceLang, setSourceLang] = useState("zh");
  const [targetLang, setTargetLang] = useState("en");
  const [genre, setGenre] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

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
      {/*
        Ingestion-method and provider-config sections land here in later phases
        (PLAN.md N3/N5/N6) — the form is built once now rather than reopened per phase.
        For now, ingestion is paste-only via ingest-api's existing endpoint (outside this
        form, once a novel exists).
      */}
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
