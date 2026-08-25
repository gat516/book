import { useState } from "react";
import { pasteChapter } from "../api";

interface Props {
  novelId: string;
  nextChapterIndex: number;
  onAdded: (chapterIndex: number) => void;
  onCancel: () => void;
}

// Paste is the only ingestion method that actually works today. Scraper (auto-follow
// "next chapter" links) and half-translated bootstrap are real, separately-planned
// features (PLAN.md N5/N6) — listed here disabled so this picker doesn't need rebuilding
// once they land, same "build the form once" call already made in NovelCreateForm.tsx.
type Method = "paste" | "scrape" | "bootstrap";

export function AddChapterForm({ novelId, nextChapterIndex, onAdded, onCancel }: Props) {
  const [method] = useState<Method>("paste");
  const [chapterIndex, setChapterIndex] = useState(nextChapterIndex);
  const [rawText, setRawText] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!rawText.trim()) return;
    setPending(true);
    setError(null);
    try {
      await pasteChapter(novelId, { chapter_index: chapterIndex, raw_text: rawText });
      onAdded(chapterIndex);
    } catch (err) {
      setError(String(err));
    } finally {
      setPending(false);
    }
  }

  return (
    <form className="add-chapter-form" onSubmit={submit}>
      <h2>Add a chapter</h2>
      <fieldset>
        <legend>Method</legend>
        <label>
          <input type="radio" checked={method === "paste"} readOnly /> Paste text
        </label>
        <label className="add-chapter-form-disabled">
          <input type="radio" disabled /> Scrape from URL (coming soon)
        </label>
        <label className="add-chapter-form-disabled">
          <input type="radio" disabled /> Half-translated bootstrap (coming soon)
        </label>
      </fieldset>
      <label>
        Chapter number
        <input
          type="number"
          min={0}
          value={chapterIndex}
          onChange={(e) => setChapterIndex(Number(e.target.value))}
        />
      </label>
      <label>
        Chapter text
        <textarea rows={10} value={rawText} onChange={(e) => setRawText(e.target.value)} required />
      </label>
      <div className="add-chapter-form-actions">
        <button type="button" onClick={onCancel} disabled={pending}>
          Cancel
        </button>
        <button type="submit" disabled={pending || !rawText.trim()}>
          {pending ? "Adding…" : "Add chapter"}
        </button>
      </div>
      {error && <p className="add-chapter-form-error">{error}</p>}
    </form>
  );
}
