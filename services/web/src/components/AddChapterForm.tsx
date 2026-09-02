import { useState } from "react";
import { pasteChapter } from "../api";
import { BootstrapChapterForm } from "./BootstrapChapterForm";
import { ScrapeForm } from "./ScrapeForm";

interface Props {
  novelId: string;
  nextChapterIndex: number;
  onAdded: (chapterIndex: number) => void;
  onCancel: () => void;
}

// Paste, scrape, and half-translated bootstrap all work (PLAN.md N5, N6). Bootstrap is a
// manual paste of a paired raw+translation text plus an explicit term mapping, distinct
// from scrape's own mode="bootstrap" (which fetches already-translated text from a site
// instead of taking it as pasted input).
type Method = "paste" | "scrape" | "bootstrap";

export function AddChapterForm({ novelId, nextChapterIndex, onAdded, onCancel }: Props) {
  const [method, setMethod] = useState<Method>("paste");
  const [chapterIndex, setChapterIndex] = useState(nextChapterIndex);
  const [rawText, setRawText] = useState("");
  const [sourceURL, setSourceURL] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!rawText.trim()) return;
    setPending(true);
    setError(null);
    try {
      await pasteChapter(novelId, {
        chapter_index: chapterIndex,
        raw_text: rawText,
        ...(sourceURL.trim() ? { source_url: sourceURL.trim() } : {}),
      });
      onAdded(chapterIndex);
    } catch (err) {
      setError(String(err));
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="add-chapter-form">
      <h2>Add a chapter</h2>
      <fieldset>
        <legend>Method</legend>
        <label>
          <input type="radio" checked={method === "paste"} onChange={() => setMethod("paste")} /> Paste text
        </label>
        <label>
          <input type="radio" checked={method === "scrape"} onChange={() => setMethod("scrape")} /> Scrape from URL
        </label>
        <label>
          <input type="radio" checked={method === "bootstrap"} onChange={() => setMethod("bootstrap")} /> I already
          have a translation
        </label>
      </fieldset>

      {method === "scrape" ? (
        <ScrapeForm novelId={novelId} onDone={() => onAdded(chapterIndex)} />
      ) : method === "bootstrap" ? (
        <BootstrapChapterForm
          novelId={novelId}
          chapterIndex={chapterIndex}
          onChapterIndexChange={setChapterIndex}
          onAdded={onAdded}
        />
      ) : (
        <form onSubmit={submit}>
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
            Source chapter URL (optional)
            <input type="url" value={sourceURL} onChange={(e) => setSourceURL(e.target.value)} />
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
      )}
    </div>
  );
}
