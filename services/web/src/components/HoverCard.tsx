import { useEffect, useState } from "react";
import { getEntity } from "../api";
import type { EntityView } from "../types";

interface Props {
  novelId: string;
  entityId: string;
  at: number;
  // Keyed by entityId only, because the Map itself is recreated whenever (novelId, at)
  // changes (see ReaderPane's `useMemo(() => new Map(), [novelId, at])`) — that recreation
  // IS the (novel_id, entity_id, at) cache key from PLAN.md §5.3/§6.1, enforced by
  // construction rather than by string-concatenating a key by hand. No bucketing, no
  // rounding: `at` is always the server-supplied ChapterResponse.at, already spoiler-safe.
  cache: Map<string, EntityView>;
  onClose: () => void;
}

export function HoverCard({ novelId, entityId, at, cache, onClose }: Props) {
  const [entity, setEntity] = useState<EntityView | null>(cache.get(entityId) ?? null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
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

  return (
    <div className="hover-card" onMouseLeave={onClose}>
      {error && <p className="hover-card-error">{error}</p>}
      {!error && !entity && <p>Loading…</p>}
      {entity && (
        <>
          <h3>{entity.canonical}</h3>
          <p className="hover-card-kind">{entity.kind}</p>
          {entity.aliases.length > 0 && (
            <p className="hover-card-aliases">Also known as: {entity.aliases.join(", ")}</p>
          )}
          <table className="hover-card-facts">
            <tbody>
              {entity.facts.map((fact) => (
                <tr key={fact.attribute}>
                  <td>{fact.attribute}</td>
                  <td>{fact.value}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}
