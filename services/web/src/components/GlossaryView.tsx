import { useCallback, useEffect, useState } from "react";
import { bootstrapGlossary, correctGlossaryTerm, deleteGlossaryTerm, getGlossary } from "../api";
import type { GlossaryResponse, GlossaryTermView } from "../types";
import { NameReviewPanel } from "./NameReviewPanel";

interface Props {
  novelId: string;
  at?: number;
}

export function GlossaryView({ novelId, at }: Props) {
  const [data, setData] = useState<GlossaryResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState("");
  const [editing, setEditing] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [source, setSource] = useState("");
  const [target, setTarget] = useState("");
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    const response = await getGlossary(novelId, at);
    setData(response);
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
      setNotice(message);
      await load();
    } catch (err) {
      setError(String(err));
    } finally {
      setSaving(false);
    }
  }

  function startEdit(term: GlossaryTermView) {
    setEditing(term.source_term);
    setDeleting(null);
    setDraft(term.target_term);
  }

  const terms = data?.terms ?? [];

  return (
    <section className="glossary-view" aria-label="Glossary management">
      <h2>Glossary</h2>
      <p className="glossary-note">
        Add names and terms to keep future translations consistent. Changes apply to future
        translation work; existing chapter text and highlights are not rewritten.
        Deleted terms stay removed until you add them again.
      </p>
      <NameReviewPanel novelId={novelId} onApproved={() => void load()} />
      <form className="glossary-add" onSubmit={(event) => {
        event.preventDefault();
        if (!source.trim() || !target.trim()) return;
        void mutate(async () => {
          await bootstrapGlossary(novelId, { terms: [{ source_term: source.trim(), target_term: target.trim() }] });
          setSource("");
          setTarget("");
        }, "Term added.");
      }}>
        <label>Source term<input value={source} onChange={(e) => setSource(e.target.value)} disabled={saving} required /></label>
        <label>Translation<input value={target} onChange={(e) => setTarget(e.target.value)} disabled={saving} required /></label>
        <button disabled={saving || !source.trim() || !target.trim()}>Add term</button>
      </form>
      {error && <div role="alert" className="glossary-error">{error} <button disabled={saving} onClick={() => {
        setError(null);
        void load().catch((err) => setError(String(err)));
      }}>Refresh glossary</button></div>}
      {notice && <p role="status">{notice}</p>}
      {!data && !error && <p>Loading glossary…</p>}
      {data && <p className="glossary-note">{terms.length} active terms visible through chapter {data.at}. Model suggestions are not included until approved by the pipeline.</p>}
      {data && terms.length === 0 && <p>No locked terms yet. Add one above; you do not need to wait for translation.</p>}
      {!!terms.length && data && <div className="glossary-table-wrap"><table>
        <thead><tr><th>Source</th><th>Translation</th><th>Locked at ch.</th><th>Actions</th></tr></thead>
        <tbody>{terms.map((term) => <tr key={term.source_term}>
          <td>{term.source_term}</td>
          <td>{editing === term.source_term ? <input aria-label={`Translation for ${term.source_term}`} value={draft} onChange={(e) => setDraft(e.target.value)} disabled={saving} autoFocus /> : term.target_term}</td>
          <td>{term.locked_at_chapter}</td>
          <td>{editing === term.source_term ? <>
            <button disabled={saving || !draft.trim()} onClick={() => void mutate(() => correctGlossaryTerm(novelId, term.source_term, { target_term: draft.trim(), at_chapter: data.at }), "Term updated.")}>Save</button>
            <button disabled={saving} onClick={() => setEditing(null)}>Cancel</button>
          </> : deleting === term.source_term ? <>
            <span>Remove this translation constraint?</span>
            <button disabled={saving} onClick={() => void mutate(() => deleteGlossaryTerm(novelId, term.source_term, data.at), "Term deleted. Existing translations are unchanged.")}>Confirm delete</button>
            <button disabled={saving} onClick={() => setDeleting(null)}>Cancel</button>
          </> : <>
            <button disabled={saving} onClick={() => startEdit(term)}>Edit</button>
            <button disabled={saving} onClick={() => { setEditing(null); setDeleting(term.source_term); }}>Delete</button>
          </>}</td>
        </tr>)}</tbody>
      </table></div>}
    </section>
  );
}
