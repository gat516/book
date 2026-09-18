import { useState } from "react";
import { saveRendering } from "../termActions";
import type { TermRenderingView } from "../types";

interface Props {
  novelId: string;
  at: number;
  renderings: TermRenderingView[];
  onChanged: (rendering: TermRenderingView) => void;
}

const METHOD_LABELS: Record<string, string> = {
  pinyin: "Pinyin",
  restored_name: "Usual spelling",
  translated_title: "Title",
  semantic_translation: "By meaning",
};

/**
 * Every name found in this chapter, with its spelling. Names still to review come first;
 * confirming or correcting one is the same glossary decision the hover card makes, and a
 * confirmed spelling stops being highlighted in the text.
 */
export function ChapterNames({ novelId, at, renderings, onChanged }: Props) {
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // One row per source term, even if two spans carry different spellings of it.
  const names = [...new Map(renderings.map((item) => [item.source_term, item])).values()].sort((a, b) =>
    Number(a.status === "locked") - Number(b.status === "locked") ||
    a.source_term.localeCompare(b.source_term, "zh"));

  async function save(rendering: TermRenderingView, target: string) {
    const spelling = target.trim();
    if (!spelling) return;
    setSaving(rendering.source_term);
    setError(null);
    try {
      onChanged(await saveRendering(novelId, rendering, spelling, at));
      setEditing(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setSaving(null);
    }
  }

  if (!names.length) return <p className="chapter-names-empty">No names have been found in this chapter yet.</p>;

  return <section className="chapter-names" aria-label="Names in this chapter">
    {error && <p role="alert" className="chapter-names-error">{error}</p>}
    <ul>
      {names.map((rendering) => {
        const spelling = rendering.target_term ?? rendering.candidates[0]?.target_term ?? "";
        const confirmed = rendering.status === "locked";
        const busy = saving === rendering.source_term;
        const method = rendering.candidates[0]?.method;
        return <li key={rendering.source_term} className={confirmed ? "is-confirmed" : undefined}>
          <span className="chapter-name-source" lang="zh">{rendering.source_term}</span>
          {editing === rendering.source_term
            ? <form className="chapter-name-edit" onSubmit={(event) => { event.preventDefault(); void save(rendering, draft); }}>
                <input aria-label={`Spelling for ${rendering.source_term}`} value={draft} autoFocus disabled={busy}
                  onChange={(event) => setDraft(event.target.value)}
                  onKeyDown={(event) => { if (event.key === "Escape") setEditing(null); }} />
                <button className="btn-primary" disabled={busy || !draft.trim() || draft.trim() === spelling && confirmed}>{busy ? "Saving…" : "Save"}</button>
                <button type="button" disabled={busy} onClick={() => setEditing(null)}>Cancel</button>
              </form>
            : <>
                <span className="chapter-name-spelling">{spelling}</span>
                <span className="chapter-name-state">{confirmed ? "Confirmed" : METHOD_LABELS[method ?? ""] ?? "To review"}</span>
                <span className="chapter-name-actions">
                  {!confirmed && <button type="button" disabled={busy || saving !== null || !spelling} onClick={() => void save(rendering, spelling)}>
                    {busy ? "Saving…" : "Confirm"}
                  </button>}
                  <button type="button" disabled={saving !== null} onClick={() => { setEditing(rendering.source_term); setDraft(spelling); }}>Edit</button>
                </span>
              </>}
        </li>;
      })}
    </ul>
  </section>;
}
