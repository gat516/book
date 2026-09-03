import { useEffect, useState } from "react";
import { getTimeline } from "../api";
import type { TimelineResponse } from "../types";
import { EventList } from "./EventList";

export function TimelineView({ novelId, onClose }: { novelId: string; onClose: () => void }) {
  const [timeline, setTimeline] = useState<TimelineResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setTimeline(null);
    setError(null);
    getTimeline(novelId).then((value) => { if (!cancelled) setTimeline(value); })
      .catch((reason) => { if (!cancelled) setError(String(reason)); });
    return () => { cancelled = true; };
  }, [novelId]);

  return <section className="timeline-view" aria-labelledby="timeline-heading">
    <div className="timeline-heading">
      <div>
        <h2 id="timeline-heading">Story timeline</h2>
        <p>Plot-significant actions through your current chapter.</p>
      </div>
      <button type="button" onClick={onClose}>Close</button>
    </div>
    {error ? <p role="alert">Could not load timeline: {error}</p>
      : timeline ? <EventList events={timeline.events} knowledge={timeline.event_knowledge} showChapter />
      : <p>Loading timeline…</p>}
  </section>;
}
