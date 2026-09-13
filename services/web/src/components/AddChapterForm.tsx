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
      <fieldset className="add-chapter-method-selector">
        <legend>How would you like to add this chapter?</legend>
        <label className="add-chapter-method-option" htmlFor="add-chapter-method-paste">
          <input
            id="add-chapter-method-paste"
            name="chapter-input-method"
            type="radio"
            checked={method === "paste"}
            onChange={() => setMethod("paste")}
          />
          <span>
            <strong>Paste original text</strong>
            <span className="add-chapter-method-description">Add the chapter text yourself for machine translation.</span>
          </span>
        </label>
        <label className="add-chapter-method-option" htmlFor="add-chapter-method-scrape">
          <input
            id="add-chapter-method-scrape"
            name="chapter-input-method"
            type="radio"
            checked={method === "scrape"}
            onChange={() => setMethod("scrape")}
          />
          <span>
            <strong>Import from webpage</strong>
            <span className="add-chapter-method-description">Fetch chapters from a supported novel site.</span>
          </span>
        </label>
        <label className="add-chapter-method-option" htmlFor="add-chapter-method-bootstrap">
          <input
            id="add-chapter-method-bootstrap"
            name="chapter-input-method"
            type="radio"
            checked={method === "bootstrap"}
            onChange={() => setMethod("bootstrap")}
          />
          <span>
            <strong>Paste original + existing translation</strong>
            <span className="add-chapter-method-description">Use a translation you already have and optionally lock its terms.</span>
          </span>
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
            Original text
            <textarea rows={10} value={rawText} onChange={(e) => setRawText(e.target.value)} required />
          </label>
          <details className="add-chapter-source-details">
            <summary>Source details</summary>
            <label>
              Original webpage
              <span className="add-chapter-field-description">
                Saved for reference and to help find later chapters; the page is not imported.
              </span>
              <input type="url" value={sourceURL} onChange={(e) => setSourceURL(e.target.value)} />
            </label>
          </details>
          <div className="add-chapter-form-actions">
            <button type="button" onClick={onCancel} disabled={pending}>
              Cancel
            </button>
            <button type="submit" className="btn-primary" disabled={pending || !rawText.trim()}>
              {pending ? "Adding…" : "Add chapter"}
            </button>
          </div>
          {error && <p className="add-chapter-form-error">{error}</p>}
        </form>
      )}
    </div>
  );
}
