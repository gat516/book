import { useEffect, useState } from "react";
import { approveCharacterName, confirmGlossaryTerm, correctGlossaryTerm, getEntity } from "../api";
import type { CharacterNameCandidate, EntityView, TermRenderingView, TermRole } from "../types";

interface Props {
  novelId: string;
  entityId: string | null;
  rendering?: TermRenderingView;
  status?: string;
  mention: string;
  at: number;
  // Keyed by entityId only, because the Map itself is recreated whenever (novelId, at)
  // changes (see ReaderPane's `useMemo(() => new Map(), [novelId, at])`) — that recreation
  // IS the (novel_id, entity_id, at) cache key from PLAN.md §5.3/§6.1, enforced by
  // construction rather than by string-concatenating a key by hand. No bucketing, no
  // rounding: `at` is always the server-supplied ChapterResponse.at, already spoiler-safe.
  cache: Map<string, EntityView>;
  onRenderingChanged?: (rendering: TermRenderingView) => void;
  onClose: () => void;
}

export function HoverCard({ novelId, entityId, rendering, status, mention, at, cache, onRenderingChanged, onClose }: Props) {
  const [entity, setEntity] = useState<EntityView | null>(entityId ? cache.get(entityId) ?? null : null);
  const [spanRendering, setSpanRendering] = useState<TermRenderingView | null>(rendering ?? null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [sourceDraft, setSourceDraft] = useState(rendering?.source_term ?? "");
  const [targetDraft, setTargetDraft] = useState(mention);
  const [roleDraft, setRoleDraft] = useState<TermRole>(rendering?.term_role || "semantic_term");

  useEffect(() => {
    if (!entityId) { setEntity(null); return; }
    const cached = cache.get(entityId);
    if (cached) {
      setEntity(cached);
      return;
    }
    let cancelled = false;
    getEntity(novelId, entityId, at)
      .then((response) => {
        if (cancelled) return;
        cache.set(entityId, response.entity);
        setEntity(response.entity);
      })
      .catch((err) => {
        if (!cancelled) setError(String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [novelId, entityId, at, cache]);

  async function chooseRendering(rendering: TermRenderingView, targetTerm: string) {
    if (!targetTerm) return;
    if (targetTerm === rendering.target_term) {
      setNotice(`“${targetTerm}” is already confirmed.`);
      return;
    }
    setSaving(rendering.source_term);
    setError(null);
    setNotice(null);
    try {
      if (rendering.status === "pending") {
        const candidate = rendering.candidates.find((item) => item.target_term === targetTerm);
        await approveCharacterName(
          novelId,
          rendering.source_term,
          targetTerm,
          roleForCandidate(candidate, rendering.term_role),
        );
      } else if (rendering.status === "unlocked") {
        await confirmGlossaryTerm(novelId, {
          source_term: rendering.source_term,
          target_term: targetTerm,
          at_chapter: at,
          term_role: rendering.term_role || "semantic_term",
        });
      } else {
        await correctGlossaryTerm(novelId, rendering.source_term, {
          target_term: targetTerm,
          at_chapter: at,
        });
      }
      const updatedRendering = { ...rendering, target_term: targetTerm, status: "locked" as const };
      setSpanRendering(updatedRendering);
      setEntity((current) => {
        if (!current) return current;
        const updated = {
          ...current,
          renderings: current.renderings.map((item) => item.source_term === rendering.source_term
            ? updatedRendering
            : item),
        };
        if (entityId) cache.set(entityId, updated);
        return updated;
      });
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
      {!entityId && <><h3>{mention}</h3><p>{status === "repair" ? "Identity unresolved — knowledge is under repair." : status === "failed" ? "Knowledge processing failed." : status === "processing" || status === "pending" ? "Knowledge processing is pending." : "Identity unresolved. You can still choose how this term should be translated."}</p></>}
      {entityId && !error && !entity && <p>Loading…</p>}
      {entity && (
        <>
          <h3>{entity.canonical}</h3>
          <p className="hover-card-kind">{entity.kind}</p>
          {entity.aliases.length > 0 && (
            <p className="hover-card-aliases">Also known as: {entity.aliases.join(", ")}</p>
          )}
          {entity.renderings.map((item) => <RenderingControl key={item.source_term}
            rendering={item} displayed={mention} saving={saving === item.source_term}
            onChoose={chooseRendering} onLeave={onClose} />)}
          {notice && <p className="hover-card-notice" role="status">{notice}</p>}
          {entity.facts.length === 0 && <p>Identity linked. No supported facts yet.</p>}
          <table className="hover-card-facts">
            <tbody>
              {entity.facts.map((fact) => (
                <tr key={fact.attribute}>
                  <td>{fact.attribute}</td>
                  <td>{fact.value} <small>Chapter {fact.source_chapter}</small></td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
      {!entity && spanRendering && <RenderingControl rendering={spanRendering} displayed={mention}
        saving={saving === spanRendering.source_term} onChoose={chooseRendering} onLeave={onClose} />}
      {!(entity ? entity.renderings.length > 0 : spanRendering) && <form className="hover-card-rendering" onSubmit={(event) => {
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
      {!entity && notice && <p className="hover-card-notice" role="status">{notice}</p>}
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
  const options = renderingOptions(rendering);
  return <section className="hover-card-rendering">
    <span>Name for future chapters <small lang="zh">{rendering.source_term}</small></span>
    {rendering.status === "locked"
      ? <small>Confirmed as “{rendering.target_term}”.</small>
      : <button disabled={saving} onClick={() => void onChoose(rendering, displayed)}>Confirm “{displayed}”</button>}
    {options.length > 0 && <select
      aria-label={`Suggested name for ${rendering.source_term}`}
      value={rendering.target_term ?? ""}
      disabled={saving}
      onChange={(event) => void onChoose(rendering, event.target.value)}
    >
      {!rendering.target_term && <option value="" disabled>Choose a suggestion…</option>}
      {options.map((option) => <option key={option} value={option}>{option}</option>)}
    </select>}
    <form onSubmit={(event) => { event.preventDefault(); void onChoose(rendering, draft.trim()); }}>
      <label>Preferred spelling<input value={draft} disabled={saving}
        onChange={(event) => setDraft(event.target.value)} /></label>
      <div className="hover-card-rendering-actions">
        <button disabled={saving || !draft.trim() || draft.trim() === rendering.target_term}>Save spelling</button>
        <button type="button" disabled={saving} onClick={onLeave}>Leave it for now</button>
      </div>
    </form>
  </section>;
}

function renderingOptions(rendering: TermRenderingView): string[] {
  return [...new Set([
    ...(rendering.target_term ? [rendering.target_term] : []),
    ...rendering.candidates.map((candidate) => candidate.target_term),
  ])];
}

function roleForCandidate(candidate: CharacterNameCandidate | undefined, fallback: TermRenderingView["term_role"]): TermRole {
  if (candidate?.method === "restored_name") return "foreign_person";
  if (candidate?.method === "translated_title") return "personal_title";
  if (candidate?.method === "semantic_translation") return "semantic_term";
  if (candidate?.method === "pinyin") return "chinese_person";
  return fallback || "chinese_person";
}
