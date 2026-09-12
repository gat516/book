import type { TermRenderingView } from "../types";

interface Props {
  renderings: TermRenderingView[];
  title?: string;
}

/** The terminology decisions attached to DISPLAY_SCAN mention spans. */
export function TermList({ renderings, title = "Terms" }: Props) {
  if (!renderings.length) return null;
  return (
    <section className="term-list" aria-label={title}>
      <h3>{title}</h3>
      <ul>
        {renderings.map((rendering) => (
          <li key={`${rendering.source_term}:${rendering.target_term ?? ""}`}>
            <span className="term-source">{rendering.source_term}</span>
            <span aria-hidden="true"> → </span>
            <span className="term-target">{rendering.target_term ?? "Needs a spelling decision"}</span>
            {rendering.status === "pending" && <small> · review pending</small>}
            {rendering.status === "unlocked" && <small> · not confirmed</small>}
            {rendering.status === "locked" && <small> · confirmed</small>}
          </li>
        ))}
      </ul>
    </section>
  );
}
