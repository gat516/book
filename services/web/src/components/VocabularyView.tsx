import { useCallback, useEffect, useState } from "react";
import { getVocabulary, mutateVocabulary } from "../api";
import type { VocabularyMutationAction, VocabularyResponse, VocabularyTermView } from "../types";

interface Props { novelId: string; }

export function VocabularyView({ novelId }: Props) {
  const [data, setData] = useState<VocabularyResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [drafts, setDrafts] = useState<Record<string, string>>({});

  const load = useCallback(async () => { setData(await getVocabulary(novelId)); }, [novelId]);
  useEffect(() => {
    let active = true;
    setData(null); setError(null); setNotice("");
    getVocabulary(novelId).then((value) => { if (active) setData(value); }).catch((err) => { if (active) setError(String(err)); });
    return () => { active = false; };
  }, [novelId]);

  async function mutate(term: VocabularyTermView, action: VocabularyMutationAction, extra: Record<string, unknown> = {}) {
    if (!data) return;
    setBusy(true); setError(null); setNotice("");
    try {
      await mutateVocabulary(novelId, { action, term_type: term.term_type, name: term.name, chapter: data.at, ...extra } as never);
      setNotice(`${term.name} updated.`); await load();
    } catch (err) { setError(String(err)); } finally { setBusy(false); }
  }
  const key = (term: VocabularyTermView) => `${term.term_type}:${term.name}`;
  const draft = (term: VocabularyTermView, suffix: string) => drafts[`${key(term)}:${suffix}`] ?? "";
  const setDraft = (term: VocabularyTermView, suffix: string, value: string) => setDrafts((old) => ({ ...old, [`${key(term)}:${suffix}`]: value }));
  const parseKinds = (value: string) => value.split(",").map((part) => part.trim()).filter(Boolean);

  return <section className="vocabulary-view" aria-label="Vocabulary management">
    <h2>Vocabulary</h2>
    <p className="glossary-note">Candidate terms are visible for inspection. Changes use your current reading position and affect future knowledge rendering.</p>
    {error && <div role="alert" className="glossary-error">{error} <button onClick={() => void load()}>Refresh</button></div>}
    {notice && <p role="status">{notice}</p>}
    {!data && !error && <p>Loading vocabulary…</p>}
    {data && <p className="glossary-note">{data.terms.length} terms visible through chapter {data.at}.</p>}
    {data && <div className="vocabulary-list">{data.terms.map((term) => <article className={`vocabulary-card vocabulary-${term.status}`} key={key(term)}>
      <header><div><strong>{term.name}</strong> <span className="vocabulary-pill">{term.status}</span></div><small>{term.term_type}</small></header>
      <p className="vocabulary-meta">Kinds: {term.kinds.join(", ") || "—"}{term.term_type === "relation" && <> · Destination kinds: {term.dst_kinds?.join(", ") || "—"}</>}</p>
      <p className="vocabulary-meta">Cardinality: {term.cardinality}{term.polarity ? ` · Polarity: ${term.polarity}` : ""}</p>
      {term.gloss && <p>{term.gloss}</p>}
      {!!term.aliases?.length && <p className="vocabulary-meta">Aliases: {term.aliases.join(", ")}</p>}
      <div className="vocabulary-actions">
        {term.status === "candidate" && <button disabled={busy} onClick={() => void mutate(term, "admit")}>Admit</button>}
        {term.status !== "banned" && term.status !== "retired" && <button disabled={busy} onClick={() => void mutate(term, "ban")}>Ban</button>}
        {term.term_type === "attribute" && <label>Cardinality<select value={draft(term, "cardinality") || term.cardinality} disabled={busy} onChange={(e) => { setDraft(term, "cardinality", e.target.value); void mutate(term, "set-cardinality", { cardinality: e.target.value }); }}><option value="single">single</option><option value="accretive">accretive</option></select></label>}
        <label>Rename alias<input value={draft(term, "alias")} disabled={busy} placeholder="old label" onChange={(e) => setDraft(term, "alias", e.target.value)} /></label>
        <button disabled={busy || !draft(term, "alias").trim()} onClick={() => void mutate(term, "rename-to-alias", { alias: draft(term, "alias").trim() })}>Add alias</button>
        <label>Gloss<input value={draft(term, "gloss") || term.gloss || ""} disabled={busy} onChange={(e) => setDraft(term, "gloss", e.target.value)} /></label>
        <button disabled={busy} onClick={() => void mutate(term, "edit-gloss", { gloss: draft(term, "gloss") })}>Save gloss</button>
        <label>Kinds<input value={draft(term, "kinds") || term.kinds.join(", ")} disabled={busy} onChange={(e) => setDraft(term, "kinds", e.target.value)} /></label>
        {term.term_type === "relation" && <label>Destination kinds<input value={draft(term, "dst_kinds") || term.dst_kinds?.join(", ") || ""} disabled={busy} onChange={(e) => setDraft(term, "dst_kinds", e.target.value)} /></label>}
        <button disabled={busy} onClick={() => void mutate(term, "set-kinds", { kinds: parseKinds(draft(term, "kinds") || term.kinds.join(",")), ...(term.term_type === "relation" ? { dst_kinds: parseKinds(draft(term, "dst_kinds") || term.dst_kinds?.join(",") || "") } : {}) })}>Save kinds</button>
      </div>
    </article>)}</div>}
  </section>;
}
