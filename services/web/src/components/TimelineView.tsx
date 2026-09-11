import { useEffect, useState } from "react";
import { getTimeline } from "../api";
import type { TimelineResponse } from "../types";
import { RecordList } from "./RecordList";

export function TimelineView({ novelId, at, onClose }: { novelId: string; at: number; onClose: () => void }) {
  const [timeline, setTimeline] = useState<TimelineResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setTimeline(null);
    setError(null);
    getTimeline(novelId, at).then((value) => { if (!cancelled) setTimeline(value); })
      .catch((reason) => { if (!cancelled) setError(String(reason)); });
    return () => { cancelled = true; };
  }, [novelId, at]);

  return <section className="timeline-view" aria-labelledby="timeline-heading">
    <div className="timeline-heading">
      <div>
        <h2 id="timeline-heading">Story timeline</h2>
        <p>Events, speeches, and promises learned through chapter {at}.</p>
      </div>
      <button type="button" onClick={onClose}>Close</button>
    </div>
    {error ? <p role="alert">Could not load timeline: {error}</p>
      : timeline ? <RecordList rows={timeline.rows} status={timeline.status} showChapter />
      : <p>Loading timeline…</p>}
  </section>;
}
