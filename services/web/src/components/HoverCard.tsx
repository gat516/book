import { useState } from "react";
import { confirmGlossaryTerm } from "../api";
import { saveRendering } from "../termActions";
import type { TermRenderingView, TermRole, WikiPageSummary } from "../types";
import { ArrowUpRight, BookOpen, Check, X } from "lucide-react";

interface Props {
  novelId: string;
  rendering?: TermRenderingView;
  mention: string;
  // Always the server-supplied ChapterResponse.at, already spoiler-safe (§0.3).
  at: number;
  onRenderingChanged?: (rendering: TermRenderingView) => void;
  onClose: () => void;
  wikiPage?: WikiPageSummary;
  wikiLoading?: boolean;
  wikiError?: string | null;
  wikiAt?: number;
  onOpenWiki?: (subject: string) => void;
}

/** A name's spelling: confirm the provisional one, correct it, or map an unknown name. */
export function HoverCard({ novelId, rendering, mention, at, onRenderingChanged, onClose, wikiPage, wikiLoading, wikiError, wikiAt, onOpenWiki }: Props) {
  const [spanRendering, setSpanRendering] = useState<TermRenderingView | null>(rendering ?? null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState<string | null>(null);
  const [sourceDraft, setSourceDraft] = useState(rendering?.source_term ?? "");
  const [targetDraft, setTargetDraft] = useState(mention);
  const [roleDraft, setRoleDraft] = useState<TermRole>(rendering?.term_role || "semantic_term");

  async function chooseRendering(rendering: TermRenderingView, targetTerm: string) {
    if (!targetTerm) return;
    if (rendering.status === "locked" && targetTerm === rendering.target_term) {
      onClose();
      return;
    }
    setSaving(rendering.source_term);
    setError(null);
    try {
      const updatedRendering = await saveRendering(novelId, rendering, targetTerm, at);
      setSpanRendering(updatedRendering);
      onRenderingChanged?.(updatedRendering);
      onClose();
    } catch (err) {
      setError(String(err));
    } finally {
      setSaving(null);
    }
  }

  async function confirmUnmapped() {
    const sourceTerm = sourceDraft.trim();
    const targetTerm = targetDraft.trim();
    if (!sourceTerm || !targetTerm) return;
    setSaving(sourceTerm);
    setError(null);
    try {
      await confirmGlossaryTerm(novelId, {
        source_term: sourceTerm,
        target_term: targetTerm,
        at_chapter: at,
        term_role: roleDraft,
      });
      const confirmed: TermRenderingView = {
        source_term: sourceTerm,
        target_term: targetTerm,
        status: "locked",
        term_role: roleDraft,
        candidates: [],
      };
      setSpanRendering(confirmed);
      onRenderingChanged?.(confirmed);
      onClose();
    } catch (err) {
      setError(String(err));
    } finally {
      setSaving(null);
    }
  }

  return (
    <div className="hover-card">
      <header className="hover-card-header"><span className="hover-monogram" aria-hidden="true">{(rendering?.source_term || mention).slice(0, 1)}</span><div><p className="eyebrow">{wikiPage?.kind ?? "IN YOUR STORY"}</p><h3>{mention}</h3></div><button type="button" className="icon-button" aria-label="Close name card" onClick={onClose}><X size={17} /></button></header>
      <div className="hover-wiki-link">{wikiPage && onOpenWiki ? <button type="button" onClick={() => { onClose(); onOpenWiki(wikiPage.subject); }}><BookOpen size={17} /><span>Open {wikiPage.kind === "character" ? "character" : "term"} wiki<small>{wikiPage.title} · {wikiPage.facts} known facts</small></span><ArrowUpRight size={16} /></button>
        : <p>{wikiLoading ? "Finding this term in your wiki…" : wikiError ? "The wiki is unavailable. Try again shortly." : "No wiki page for this term at this chapter yet."}</p>}</div>
      {error && <p className="hover-card-error" role="alert">{error}</p>}
      {spanRendering && <RenderingControl rendering={spanRendering} displayed={mention}
        saving={saving === spanRendering.source_term} onChoose={chooseRendering} onLeave={onClose} />}
      {!spanRendering && <form className="hover-card-rendering" onSubmit={(event) => {
        event.preventDefault();
        void confirmUnmapped();
      }}>
        <strong>Confirm or correct this spelling</strong>
        <small>Enter the original source spelling so future source chapters can be matched safely.</small>
        <label>Original source term<input value={sourceDraft} onChange={(event) => setSourceDraft(event.target.value)} required /></label>
        <label>Preferred spelling<input value={targetDraft} onChange={(event) => setTargetDraft(event.target.value)} required /></label>
        <label>Term type<select value={roleDraft} onChange={(event) => setRoleDraft(event.target.value as TermRole)}>
          <option value="chinese_person">Chinese personal name</option>
          <option value="foreign_person">Foreign or transcribed name</option>
          <option value="personal_title">Personal title or epithet</option>
          <option value="semantic_term">Place, group, object, technique, or other term</option>
        </select></label>
        <div className="hover-card-rendering-actions">
          <button className="btn-primary" disabled={!!saving || !sourceDraft.trim() || !targetDraft.trim()}>{saving ? "Saving…" : "Confirm spelling"}</button>
          <button type="button" onClick={onClose}>Not now</button>
        </div>
      </form>}
      <p className="hover-card-boundary">Story knowledge through chapter {wikiAt ?? at}</p>
    </div>
  );
}

function RenderingControl({ rendering, displayed, saving, onChoose, onLeave }: {
  rendering: TermRenderingView;
  displayed: string;
  saving: boolean;
  onChoose: (rendering: TermRenderingView, targetTerm: string) => Promise<void>;
  onLeave: () => void;
}) {
  const [draft, setDraft] = useState(rendering.target_term ?? displayed);
  const [editing, setEditing] = useState(false);
  const chosen = rendering.target_term ?? displayed;
  return <section className="hover-card-rendering">
    <span>Term spelling <small lang="zh">{rendering.source_term}</small></span>
    {rendering.status === "locked"
      ? <small className="spelling-confirmed"><Check size={14} /> Spelling confirmed</small>
      : <>
          <small>Using “{chosen}” provisionally. Confirm it or enter a correction.</small>
          <button className="btn-primary" disabled={saving} onClick={() => void onChoose(rendering, chosen)}><Check size={15} />{saving ? "Saving…" : `Confirm “${chosen}”`}</button>
        </>}
    {!editing && <button type="button" className="text-button" onClick={() => setEditing(true)}>Change spelling</button>}
    {editing && <form onSubmit={(event) => { event.preventDefault(); void onChoose(rendering, draft.trim()); }}>
      <label>Preferred spelling<input value={draft} disabled={saving}
        onChange={(event) => setDraft(event.target.value)} /></label>
      <div className="hover-card-rendering-actions">
        <button className="btn-primary" disabled={saving || !draft.trim() || (rendering.status === "locked" && draft.trim() === rendering.target_term)}>{saving ? "Saving…" : "Save spelling"}</button>
        <button type="button" onClick={onLeave}>Cancel</button>
      </div>
    </form>}
  </section>;
}
