import type { RecordView, RecordsStatus } from "../types";

function label(type: string): string {
  return type.replaceAll("_", " ").toLowerCase().replace(/\b\w/g, c => c.toUpperCase());
}

export function RecordList({ rows, status, onEntity, showChapter = false, title }: { rows: RecordView[]; status?: RecordsStatus; onEntity?: (id: string, surface: string) => void; showChapter?: boolean; title?: string }) {
  if (!rows.length) return <section className="record-list" aria-label="Chapter records">
    {title && <h3>{title}</h3>}
    <p className="records-empty">{!status ? "No supported records are known at your reading progress yet." : status.extraction_status === "ready" ? "No records in this chapter." : status.extraction_status === "failed" ? "Record extraction failed; retry is available in Book settings." : "Knowledge is still being extracted…"}</p>
  </section>;
  return <section className="record-list" aria-label="Chapter records">
    {title && <h3>{title}</h3>}
    {rows.map(row => <article className="record-card" key={row.id}>
      <header><strong>{label(row.type)}</strong>{showChapter && <span>Learned in chapter {row.source_chapter}</span>}{row.valid_from_chapter !== null && <span>Story time: chapter {row.valid_from_chapter}</span>}{row.temporal_qualifier && <em>{temporalLabel(row.temporal_qualifier)}</em>}</header>
      {row.values.map(value => <p key={value.field}><b>{label(value.field)}:</b> {value.rendered ? <><span>{value.rendered}</span> <small>(source: {value.source})</small></> : <><span>{value.source}</span>{value.render_status === "failed" && <small> (source language; English rendering failed)</small>}</>}</p>)}
      {!!row.participants.length && <p className="record-participants"><b>Participants:</b> {row.participants.map((person, i) => <span key={`${person.ordinal ?? i}-${person.surface}`}>{i > 0 && ", "}{person.entity_id && onEntity ? <button type="button" onClick={() => onEntity(person.entity_id!, person.surface)}>{person.surface}</button> : person.entity_id ? <span>{person.surface}</span> : <span className="record-unresolved" title={person.unresolved_reason ?? "Identity unresolved"}>{person.surface} <small>(unresolved)</small></span>}</span>)}</p>}
      {!!row.evidence.length && <details><summary>Source evidence ({row.evidence.length})</summary>{row.evidence.map(e => <blockquote key={`${e.run_id ?? ""}:${e.passage_id}`}>{e.quote || e.text || "Passage evidence"} {e.quote && e.text && e.quote !== e.text && <small> · full passage: {e.text}</small>} <small>chapter {e.chapter}{e.ordinal !== undefined ? ` · passage ${e.ordinal + 1}` : ""}</small></blockquote>)}</details>}
    </article>)}
  </section>;
}

function temporalLabel(value: string): string {
  const normalized = value.toLowerCase().replaceAll("_", " ");
  return normalized.includes("earlier") ? "Recounted from earlier" : normalized;
}
