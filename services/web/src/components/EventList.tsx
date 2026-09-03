import type { EventView, KnowledgeStatus } from "../types";

interface Props {
  events: EventView[];
  knowledge: KnowledgeStatus;
  showChapter?: boolean;
  emptyText?: string;
}

const statusLabel: Record<EventView["status"], string> = {
  completed: "Completed",
  attempted: "Attempted",
  prevented: "Prevented",
};

export function EventList({ events, knowledge, showChapter = false, emptyText = "No plot-significant events were recorded." }: Props) {
  if (knowledge.status === "unavailable") {
    return <p className="event-empty">Event extraction has not been reviewed and activated yet.</p>;
  }
  if (knowledge.status === "pending" || knowledge.status === "processing") {
    return <p role="status" className="event-empty">Finding plot-significant actions…</p>;
  }
  if (knowledge.status === "failed") {
    return <p role="status" className="event-empty">Event extraction failed; the chapter text is still available.</p>;
  }
  if (events.length === 0) return <p className="event-empty">{emptyText}</p>;

  return <ol className="event-list">
    {events.map((event) => <li key={event.id} className="event-card">
      <div className="event-card-heading">
        {showChapter && <span>Chapter {event.chapter_index}</span>}
        <span className={`event-status event-status-${event.status}`}>{statusLabel[event.status]}</span>
        <span className="event-type">{event.event_type.replaceAll("_", " ")}</span>
      </div>
      <strong>{event.action}</strong>
      <p>{event.summary}</p>
      {event.arguments.length > 0 && <dl className="event-arguments">
        {event.arguments.map((argument, index) => <div key={`${argument.role}:${argument.surface}:${index}`}>
          <dt>{argument.role.replaceAll("_", " ")}</dt>
          <dd>{argument.entity?.canonical ?? argument.surface}</dd>
        </div>)}
      </dl>}
      {event.result && <p className="event-result"><span>Result:</span> {event.result}</p>}
      <details className="event-evidence">
        <summary>Evidence</summary>
        <q>{event.evidence.quote}</q>
      </details>
    </li>)}
  </ol>;
}
