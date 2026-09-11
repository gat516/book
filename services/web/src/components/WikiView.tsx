import { useEffect, useState } from "react";
import { getWiki } from "../api";
import type { WikiResponse } from "../types";

export function WikiView({ novelId, at, onClose }: { novelId: string; at: number; onClose: () => void }) {
  const [wiki, setWiki] = useState<WikiResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { let gone = false; getWiki(novelId, at).then(v => { if (!gone) setWiki(v); }).catch(e => { if (!gone) setError(String(e)); }); return () => { gone = true; }; }, [novelId, at]);
  return <section className="wiki-view"><div className="timeline-heading"><h2>Who’s who</h2><button onClick={onClose}>Close</button></div>
    {error ? <p role="alert">Could not load wiki: {error}</p> : !wiki ? <p>Loading wiki…</p> : <ul>{wiki.entities.map(entity => <li key={entity.id}><strong>{entity.canonical}</strong>{entity.kind && <span> · {entity.kind}</span>}<small> · first seen chapter {entity.first_seen_chapter}</small></li>)}</ul>}
  </section>;
}
