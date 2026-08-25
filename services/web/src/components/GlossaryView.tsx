import { useEffect, useState } from "react";
import { correctGlossaryTerm, getGlossary } from "../api";
import type { GlossaryTermView } from "../types";

interface Props {
  novelId: string;
  at: number;
}

export function GlossaryView({ novelId, at }: Props) {
  const [terms, setTerms] = useState<GlossaryTermView[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);

  function load() {
    getGlossary(novelId, at)
      .then((response) => setTerms(response.terms))
      .catch((err) => setError(String(err)));
  }

  useEffect(load, [novelId, at]);

  function startEdit(term: GlossaryTermView) {
    setEditing(term.source_term);
    setDraft(term.target_term);
  }

  async function save(sourceTerm: string) {
    if (!draft.trim()) return;
    setSaving(true);
    setError(null);
    try {
      await correctGlossaryTerm(novelId, sourceTerm, { target_term: draft, at_chapter: at });
      setEditing(null);
      load();
    } catch (err) {
      setError(String(err));
    } finally {
      setSaving(false);
    }
  }

  if (error) return <p className="glossary-error">Could not load glossary: {error}</p>;
  if (!terms) return <p>Loading glossary…</p>;

  return (
    <div className="glossary-view">
      <h2>Glossary</h2>
      <p className="glossary-note">
        Corrections apply going forward only — chapters you've already read with the old term aren't updated.
      </p>
      {terms.length === 0 && <p>No terms locked yet at this point in the story.</p>}
      {terms.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Source</th>
              <th>Target</th>
              <th>Locked at ch.</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {terms.map((term) => (
              <tr key={term.source_term}>
                <td>{term.source_term}</td>
                <td>
                  {editing === term.source_term ? (
                    <input value={draft} onChange={(e) => setDraft(e.target.value)} autoFocus />
                  ) : (
                    term.target_term
                  )}
                </td>
                <td>{term.locked_at_chapter}</td>
                <td>
                  {editing === term.source_term ? (
                    <>
                      <button onClick={() => save(term.source_term)} disabled={saving || !draft.trim()}>
                        {saving ? "Saving…" : "Save"}
                      </button>
                      <button onClick={() => setEditing(null)} disabled={saving}>
                        Cancel
                      </button>
                    </>
                  ) : (
                    <button onClick={() => startEdit(term)}>Edit</button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
