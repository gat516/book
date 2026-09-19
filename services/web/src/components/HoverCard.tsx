import { useState } from "react";
import { confirmGlossaryTerm } from "../api";
import { saveRendering } from "../termActions";
import type { TermRenderingView, TermRole } from "../types";

interface Props {
  novelId: string;
  rendering?: TermRenderingView;
  mention: string;
  // Always the server-supplied ChapterResponse.at, already spoiler-safe (§0.3).
  at: number;
  onRenderingChanged?: (rendering: TermRenderingView) => void;
  onClose: () => void;
}

/** A name's spelling: confirm the provisional one, correct it, or map an unknown name. */
export function HoverCard({ novelId, rendering, mention, at, onRenderingChanged, onClose }: Props) {
  const [spanRendering, setSpanRendering] = useState<TermRenderingView | null>(rendering ?? null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [sourceDraft, setSourceDraft] = useState(rendering?.source_term ?? "");
  const [targetDraft, setTargetDraft] = useState(mention);
  const [roleDraft, setRoleDraft] = useState<TermRole>(rendering?.term_role || "semantic_term");

  async function chooseRendering(rendering: TermRenderingView, targetTerm: string) {
    if (!targetTerm) return;
    if (rendering.status === "locked" && targetTerm === rendering.target_term) {
      setNotice(`“${targetTerm}” is already confirmed.`);
      return;
    }
    setSaving(rendering.source_term);
    setError(null);
    setNotice(null);
    try {
      const updatedRendering = await saveRendering(novelId, rendering, targetTerm, at);
      setSpanRendering(updatedRendering);
      onRenderingChanged?.(updatedRendering);
      setNotice(`Now shown as “${targetTerm}” in mapped chapters. Future translations will use it too.`);
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
    setNotice(null);
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
      setNotice(`Now shown as “${targetTerm}” in mapped chapters. Future translations will use it too.`);
    } catch (err) {
      setError(String(err));
    } finally {
      setSaving(null);
    }
  }

  return (
    <div className="hover-card" onMouseLeave={onClose}>
      {error && <p className="hover-card-error">{error}</p>}
      <h3>{mention}</h3>
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
          <button disabled={!!saving || !sourceDraft.trim() || !targetDraft.trim()}>Confirm spelling</button>
          <button type="button" disabled={!!saving} onClick={onClose}>Leave it for now</button>
        </div>
      </form>}
      {notice && <p className="hover-card-notice" role="status">{notice}</p>}
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
  const chosen = rendering.target_term ?? displayed;
  return <section className="hover-card-rendering">
    <span>Term spelling <small lang="zh">{rendering.source_term}</small></span>
    {rendering.status === "locked"
      ? <small>Confirmed as “{rendering.target_term}”.</small>
      : <>
          <small>Using “{chosen}” provisionally. Confirm it or enter a correction.</small>
          <button disabled={saving} onClick={() => void onChoose(rendering, chosen)}>Confirm “{chosen}”</button>
        </>}
    <form onSubmit={(event) => { event.preventDefault(); void onChoose(rendering, draft.trim()); }}>
      <label>Preferred spelling<input value={draft} disabled={saving}
        onChange={(event) => setDraft(event.target.value)} /></label>
      <div className="hover-card-rendering-actions">
        <button disabled={saving || !draft.trim() || (rendering.status === "locked" && draft.trim() === rendering.target_term)}>Save spelling</button>
        <button type="button" disabled={saving} onClick={onLeave}>Leave it for now</button>
      </div>
    </form>
  </section>;
}
