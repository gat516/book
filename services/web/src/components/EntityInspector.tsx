import { useEffect, useId, useRef, useState } from "react";
import { getEntity } from "../api";
import type { EntityView } from "../types";
import { GlossaryView } from "./GlossaryView";
import { RecordList } from "./RecordList";
import { TermList } from "./TermList";

interface Props {
  novelId: string;
  entityId: string | null;
  status?: string;
  mention: string;
  at: number;
  cache: Map<string, EntityView>;
  onClose: () => void;
}

export function EntityInspector({ novelId, entityId, status, mention, at, cache, onClose }: Props) {
  const dialog = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const editorId = useId();
  const [entity, setEntity] = useState<EntityView | null>(entityId ? cache.get(entityId) ?? null : null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [editing, setEditing] = useState(false);

  useEffect(() => {
    const element = dialog.current!;
    element.showModal(); // Native focus trap, Escape, and focus restoration to the name.
    return () => { element.close(); };
  }, []);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    if (!entityId) { setEntity(null); return; }
    const cached = cache.get(entityId);
    if (cached) {
      setEntity(cached);
      return;
    }
    setEntity(null);
    // Same server-authorized chapter boundary as hover cards (§0.3). No new graph writes.
    getEntity(novelId, entityId, at).then((response) => {
      if (cancelled) return;
      cache.set(entityId, response.entity);
      setEntity(response.entity);
    }).catch((err) => { if (!cancelled) setError(String(err)); });
    return () => { cancelled = true; };
  }, [novelId, entityId, at, cache, attempt]);

  return (
    <dialog ref={dialog} className="entity-inspector" aria-labelledby={titleId} onClose={() => {
      // StrictMode replays effects; ignore the prior cleanup's queued close event if
      // the dialog has already reopened. User Escape/Close leaves it actually closed.
      if (!dialog.current?.open) onClose();
    }}>
      <header className="entity-inspector-header">
        <h2 id={titleId}>{mention}</h2>
        <button type="button" autoFocus onClick={() => dialog.current?.close()} aria-label="Close entity details">Close</button>
      </header>
      <p className="entity-inspector-context">Known through chapter {at}</p>
      {error && <div role="alert">Could not load entity: {error} <button onClick={() => setAttempt((n) => n + 1)}>Retry</button></div>}
      {!entityId && <p>{status === "repair" ? "Identity unresolved. Knowledge is under repair; unverified facts are withheld." : status === "processing" || status === "pending" ? "Knowledge processing is pending. This name remains clickable." : status === "failed" ? "Knowledge processing failed. Identity is unresolved." : "Identity unresolved. No supported link yet."}</p>}
      {entityId && !entity && !error && <p role="status">Loading entity…</p>}
      {entity && <>
        <p><strong>{entity.canonical}</strong> · {entity.kind} · First seen in chapter {entity.first_seen_chapter}</p>
        {entity.aliases.length > 0 && <p>Also known as: {entity.aliases.join(", ")}</p>}
        <h3>Facts and records</h3>
        {entity.records?.length ? <RecordList rows={entity.records} /> : <p>No supported records are known at your reading progress yet.</p>}
        <TermList renderings={entity.renderings} title="Terms" />
        <button className="entity-inspector-edit" aria-expanded={editing} aria-controls={editorId} onClick={() => setEditing((value) => !value)}>
          {editing ? "Hide glossary editor" : "Edit glossary terms"}
        </button>
        <div id={editorId} hidden={!editing}>
          {editing && <GlossaryView novelId={novelId} at={at} entity={entity} suggestedTarget={mention} />}
        </div>
      </>}
    </dialog>
  );
}
