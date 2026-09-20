import { useCallback, useEffect, useState } from "react";
import { bootstrapGlossary, correctGlossaryTerm, deleteGlossaryTerm, getGlossary } from "../api";
import type { GlossaryResponse, GlossaryTermView } from "../types";
import { NameReviewPanel } from "./NameReviewPanel";

interface Props {
  novelId: string;
  at?: number;
}

/**
 * The book's terminology: confirmed spellings first, then the ones still waiting for a
 * decision. Both are dense lists -- a book carries a handful of confirmed terms and can
 * carry hundreds of pending ones, so a row shows its controls only once it is selected.
 */
export function GlossaryView({ novelId, at }: Props) {
  const [data, setData] = useState<GlossaryResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [query, setQuery] = useState("");
  const [adding, setAdding] = useState(false);
  const [source, setSource] = useState("");
  const [target, setTarget] = useState("");
  const [saving, setSaving] = useState(false);
  const [pending, setPending] = useState(0);

  const load = useCallback(async () => {
    setData(await getGlossary(novelId, at));
  }, [novelId, at]);

  useEffect(() => {
    let active = true;
    setData(null);
    setError(null);
    getGlossary(novelId, at).then((response) => {
      if (active) setData(response);
    }).catch((err) => { if (active) setError(String(err)); });
    return () => { active = false; };
  }, [novelId, at]);

  async function mutate(action: () => Promise<unknown>, message: string) {
    setSaving(true);
    setError(null);
    setNotice("");
    try {
      await action();
      setEditing(null);
      setDeleting(null);
      setSelected(null);
      setNotice(message);
      await load();
    } catch (err) {
      setError(String(err));
    } finally {
      setSaving(false);
    }
  }

  function select(term: GlossaryTermView) {
    setSelected((current) => current === term.source_term ? null : term.source_term);
    setEditing(null);
    setDeleting(null);
    setDraft(term.target_term);
  }

  const needle = query.trim().toLowerCase();
  const all = data?.terms ?? [];
  const terms = [...(needle
    ? all.filter((term) => term.source_term.toLowerCase().includes(needle) || term.target_term.toLowerCase().includes(needle))
    : all)].sort((a, b) => a.target_term.localeCompare(b.target_term));

  return (
    <section className="glossary-view" aria-label="Glossary">
      <div className="glossary-head">
        <h2>Glossary</h2>
        <p className="glossary-counts">
          <span>{all.length} confirmed</span>
          {pending > 0 && <span>{pending} to review</span>}
          {data && <span className="glossary-gate">through chapter {data.at}</span>}
        </p>
      </div>

      <div className="glossary-tools">
        <label className="glossary-filter">
          <span className="visually-hidden">Filter terms</span>
          <input type="search" value={query} placeholder="Filter terms" disabled={!data}
            onChange={(event) => setQuery(event.target.value)} />
        </label>
        <button type="button" aria-expanded={adding} onClick={() => setAdding((open) => !open)}>
          {adding ? "Close" : "Add term"}
        </button>
      </div>

      {adding && <form className="glossary-add" onSubmit={(event) => {
        event.preventDefault();
        if (!source.trim() || !target.trim()) return;
        void mutate(async () => {
          await bootstrapGlossary(novelId, { terms: [{ source_term: source.trim(), target_term: target.trim() }] });
          setSource("");
          setTarget("");
          setAdding(false);
        }, "Term added. Future chapters will use it.");
      }}>
        <label>Source term<input value={source} lang="zh" onChange={(e) => setSource(e.target.value)} disabled={saving} required /></label>
        <label>Spelling<input value={target} onChange={(e) => setTarget(e.target.value)} disabled={saving} required /></label>
        <button disabled={saving || !source.trim() || !target.trim()}>Add term</button>
        <p className="term-hint">Added terms hold for future translation work. Saved chapters keep the text they already have.</p>
      </form>}

      {error && <div role="alert" className="term-error">{error} <button disabled={saving} onClick={() => {
        setError(null);
        void load().catch((err) => setError(String(err)));
      }}>Try again</button></div>}
      {notice && <p role="status" className="term-notice">{notice}</p>}

      {!data && !error && <p className="term-empty">Loading…</p>}

      {data && <section className="term-group" aria-label="Confirmed spellings">
        <div className="term-head">
          <h3>Confirmed</h3>
          {terms.length !== all.length && <span className="term-count">{terms.length} shown</span>}
        </div>
        {!all.length ? <p className="term-empty">No confirmed terms yet. Add one above, or confirm a spelling below.</p>
          : !terms.length ? <p className="term-empty">No term matches “{query}”.</p>
          : <ul className="term-list">{terms.map((term) => {
            const isSelected = selected === term.source_term;
            return <li key={term.source_term} className={`term-row${isSelected ? " is-open" : ""}`}>
              <div className="term-line">
                <button type="button" className="term-pick" aria-expanded={isSelected} onClick={() => select(term)}>
                  <span className="term-source" lang="zh">{term.source_term}</span>
                  <span className="term-target">{term.target_term}</span>
                  {term.locked_at_chapter > 0 && <span className="term-chapter">ch {term.locked_at_chapter}</span>}
                </button>
              </div>
              {isSelected && <div className="term-editor">
                {editing === term.source_term ? <form onSubmit={(event) => {
                  event.preventDefault();
                  void mutate(() => correctGlossaryTerm(novelId, term.source_term, { target_term: draft.trim(), at_chapter: data.at }),
                    "Spelling updated. Future chapters will use it.");
                }}>
                  <label>Spelling
                    <input aria-label={`Spelling for ${term.source_term}`} value={draft} autoFocus disabled={saving}
                      onChange={(e) => setDraft(e.target.value)} />
                  </label>
                  <div className="term-editor-actions">
                    <button disabled={saving || !draft.trim()}>Save spelling</button>
                    <button type="button" disabled={saving} onClick={() => setEditing(null)}>Cancel</button>
                  </div>
                </form>
                : deleting === term.source_term ? <div className="term-editor-actions">
                  <span>Remove this spelling? Saved chapters keep their text.</span>
                  <button className="btn-danger" disabled={saving}
                    onClick={() => void mutate(() => deleteGlossaryTerm(novelId, term.source_term, data.at), "Term removed.")}>Remove</button>
                  <button type="button" disabled={saving} onClick={() => setDeleting(null)}>Keep</button>
                </div>
                : <div className="term-editor-actions">
                  <button type="button" disabled={saving} onClick={() => { setEditing(term.source_term); setDraft(term.target_term); }}>Edit spelling</button>
                  <button type="button" disabled={saving} onClick={() => setDeleting(term.source_term)}>Remove</button>
                </div>}
              </div>}
            </li>;
          })}</ul>}
      </section>}

      <NameReviewPanel novelId={novelId} filter={query} onCount={setPending} onApproved={() => void load()} />
    </section>
  );
}
