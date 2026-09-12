import { useEffect, useState } from "react";
import { confirmGlossaryTerm, correctGlossaryTerm, deleteGlossaryTerm } from "../api";
import type { TermRenderingView } from "../types";

interface Props {
  novelId: string;
  at: number;
  renderings: TermRenderingView[];
}

/**
 * Chapter-scoped terminology editing. This writes glossary decisions only; it never
 * edits frozen translated text or binds an entity identity. Pending character-name
 * proposals remain in NameReviewPanel, which has the candidate/role contract needed to
 * approve them safely.
 */
export function ChapterTermsReview({ novelId, at, renderings }: Props) {
  const [items, setItems] = useState(renderings);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    setItems(renderings);
    setDrafts(Object.fromEntries(renderings.map((item) => [item.source_term, item.target_term ?? ""])));
  }, [renderings]);

  async function save(item: TermRenderingView) {
    const target = (drafts[item.source_term] ?? "").trim();
    if (!target) return;
    setBusy(item.source_term);
    setError(null);
    setNotice(null);
    try {
      if (item.status === "unlocked") {
        await confirmGlossaryTerm(novelId, {
          source_term: item.source_term,
          target_term: target,
          at_chapter: at,
          term_role: item.term_role || "semantic_term",
        });
      } else {
        await correctGlossaryTerm(novelId, item.source_term, {
          target_term: target,
          at_chapter: at,
        });
      }
      setItems((current) => current.map((entry) => entry.source_term === item.source_term
        ? { ...entry, target_term: target, status: "locked" }
        : entry));
      setNotice(`Saved “${item.source_term}” as “${target}”. Existing prose is unchanged.`);
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(null);
    }
  }

  async function remove(item: TermRenderingView) {
    if (item.status !== "locked" || !window.confirm(`Remove the glossary constraint for “${item.source_term}”? Existing prose stays unchanged.`)) return;
    setBusy(item.source_term);
    setError(null);
    setNotice(null);
    try {
      await deleteGlossaryTerm(novelId, item.source_term, at);
      setItems((current) => current.filter((entry) => entry.source_term !== item.source_term));
      setNotice(`Removed “${item.source_term}”. Existing prose is unchanged.`);
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(null);
    }
  }

  return <section className="chapter-terms-review" aria-label="Chapter term review">
    <h3>Terms in this chapter</h3>
    <p className="glossary-note">Confirm or correct terminology for future translations. Pending character-name proposals are reviewed above; these controls never rewrite saved prose.</p>
    {error && <p role="alert" className="reader-pane-error">{error}</p>}
    {notice && <p role="status">{notice}</p>}
    {!items.length ? <p className="term-empty">No terminology decisions are recorded here yet.</p> : <div className="chapter-terms-list">
      {items.map((item) => <article className="chapter-term-row" key={item.source_term}>
        <div><strong lang="zh">{item.source_term}</strong><small> · {item.status === "pending" ? "pending name review" : item.status === "unlocked" ? "not confirmed" : "confirmed"}</small></div>
        {item.status === "pending" ? <p className="glossary-note">Choose a candidate in the pending name review above.</p> : <>
          <label>Preferred translation<input value={drafts[item.source_term] ?? ""} onChange={(event) => setDrafts((current) => ({ ...current, [item.source_term]: event.target.value }))} disabled={busy === item.source_term} /></label>
          <div className="knowledge-actions">
            <button type="button" disabled={busy !== null || !drafts[item.source_term]?.trim()} onClick={() => void save(item)}>{busy === item.source_term ? "Saving…" : item.status === "unlocked" ? "Confirm term" : "Save correction"}</button>
            {item.status === "locked" && <button type="button" disabled={busy !== null} onClick={() => void remove(item)}>Remove constraint</button>}
          </div>
        </>}
      </article>)}
    </div>}
  </section>;
}

function errorMessage(reason: unknown): string {
  return reason instanceof Error ? reason.message : String(reason);
}
