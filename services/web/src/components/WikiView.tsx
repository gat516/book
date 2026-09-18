import { EntityInspector } from "./EntityInspector";
import { RecordList } from "./RecordList";
import { useKnowledgeRevision } from "../knowledgeUpdates";
import { useEffect, useMemo, useState } from "react";
import { getWiki } from "../api";
import type { EntityView, WikiResponse } from "../types";

export function WikiView({ novelId, at, onClose }: { novelId: string; at: number; onClose: () => void }) {
  const revision = useKnowledgeRevision(novelId);
  const [wiki, setWiki] = useState<WikiResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<{ id: string; name: string } | null>(null);
  const cache = useMemo(() => new Map<string, EntityView>(), [novelId, at, revision]);
  useEffect(() => { let gone = false; setWiki(null); setError(null); getWiki(novelId, at).then(v => { if (!gone) setWiki(v); }).catch(e => { if (!gone) setError(String(e)); }); return () => { gone = true; }; }, [novelId, at, revision]);
  return <section className="wiki-view"><div className="timeline-heading"><h2>Who’s who</h2><button onClick={onClose}>Close</button></div>
    {error ? <p role="alert">Could not load wiki: {error}</p> : !wiki ? <p>Loading wiki…</p> : <ul>{wiki.entities.map(entity => <li key={entity.id}><button type="button" className="link-button" onClick={() => setSelected({ id: entity.id, name: entity.canonical })}><strong>{entity.canonical}</strong></button>{entity.kind && <span> · {entity.kind}</span>}<small> · first seen chapter {entity.first_seen_chapter}</small></li>)}</ul>}
    {wiki && <RecordList rows={wiki.rows ?? []} status={wiki.status} title="Facts and relationships" />}
    {selected && <EntityInspector key={`${novelId}:${at}:${selected.id}`} novelId={novelId} entityId={selected.id}
      mention={selected.name} at={at} cache={cache} onClose={() => setSelected(null)} />}
  </section>;
}
