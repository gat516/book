import { useState } from "react";
import { bootstrapGlossary, pasteChapter } from "../api";
import type { BootstrapGlossaryTermInput } from "../types";

interface Props {
  novelId: string;
  chapterIndex: number;
  onChapterIndexChange: (index: number) => void;
  onAdded: (chapterIndex: number) => void;
}

// PLAN.md Phase N6: a human pastes an already-existing raw+translation pair (not a
// scrape — that's ScrapeForm's mode="bootstrap") plus the term mapping a fan translation
// implies, so later chapters MT against those exact locked terms instead of RESOLVE
// inventing its own. The term table's shape mirrors GlossaryView's editable rows.
export function BootstrapChapterForm({ novelId, chapterIndex, onChapterIndexChange, onAdded }: Props) {
  const [rawText, setRawText] = useState("");
  const [translatedText, setTranslatedText] = useState("");
  const [sourceURL, setSourceURL] = useState("");
  const [terms, setTerms] = useState<BootstrapGlossaryTermInput[]>([{ source_term: "", target_term: "" }]);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function updateTerm(i: number, field: keyof BootstrapGlossaryTermInput, value: string) {
    setTerms((prev) => prev.map((t, idx) => (idx === i ? { ...t, [field]: value } : t)));
  }

  function addTermRow() {
    setTerms((prev) => [...prev, { source_term: "", target_term: "" }]);
  }

  function removeTermRow(i: number) {
    setTerms((prev) => prev.filter((_, idx) => idx !== i));
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!rawText.trim() || !translatedText.trim()) return;
    setPending(true);
    setError(null);
    try {
      const filledTerms = terms.filter((t) => t.source_term.trim() && t.target_term.trim());
      if (filledTerms.length > 0) {
        // Lock the glossary FIRST: if this fails, the chapter paste below never happens,
        // so a chapter never lands with translated_text but no matching glossary lock
        // (the ordering that keeps a half-done bootstrap from looking like a full one).
        await bootstrapGlossary(novelId, { terms: filledTerms });
      }
      await pasteChapter(novelId, {
        chapter_index: chapterIndex,
        raw_text: rawText,
        translated_text: translatedText,
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
    <form className="bootstrap-chapter-form" onSubmit={submit}>
      <p className="bootstrap-chapter-form-hint">
        Paste a chapter you already have a translation for, plus the source→target terms that translation uses.
        This chapter skips machine translation entirely; later chapters you paste with only raw text will be
        machine-translated against these locked terms.
      </p>
      <label>
        Chapter number
        <input
          type="number"
          min={0}
          value={chapterIndex}
          onChange={(e) => onChapterIndexChange(Number(e.target.value))}
        />
      </label>
      <label>
        Source chapter URL (optional)
        <input type="url" value={sourceURL} onChange={(e) => setSourceURL(e.target.value)} />
      </label>
      <label>
        Original text
        <textarea rows={8} value={rawText} onChange={(e) => setRawText(e.target.value)} required />
      </label>
      <label>
        Existing translation
        <textarea rows={8} value={translatedText} onChange={(e) => setTranslatedText(e.target.value)} required />
      </label>
      <fieldset>
        <legend>Term mapping (optional, but locks these terms for every later chapter)</legend>
        <table>
          <thead>
            <tr>
              <th>Source term</th>
              <th>Target term</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {terms.map((term, i) => (
              <tr key={i}>
                <td>
                  <input value={term.source_term} onChange={(e) => updateTerm(i, "source_term", e.target.value)} />
                </td>
                <td>
                  <input value={term.target_term} onChange={(e) => updateTerm(i, "target_term", e.target.value)} />
                </td>
                <td>
                  <button type="button" onClick={() => removeTermRow(i)} disabled={terms.length === 1}>
                    Remove
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <button type="button" onClick={addTermRow}>
          Add term
        </button>
      </fieldset>
      <div className="bootstrap-chapter-form-actions">
        <button type="submit" disabled={pending || !rawText.trim() || !translatedText.trim()}>
          {pending ? "Adding…" : "Add chapter"}
        </button>
      </div>
      {error && <p className="bootstrap-chapter-form-error">{error}</p>}
    </form>
  );
}
