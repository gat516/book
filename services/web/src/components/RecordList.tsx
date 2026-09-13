import type { RecordView, RecordsStatus } from "../types";

function label(type: string): string {
  return type.replaceAll("_", " ").toLowerCase().replace(/\b\w/g, c => c.toUpperCase());
}

export function RecordList({ rows, status, onEntity, showChapter = true, title }: { rows: RecordView[]; status?: RecordsStatus; onEntity?: (id: string, surface: string) => void; showChapter?: boolean; title?: string }) {
  if (!rows.length) return <section className="record-list" aria-label="Chapter records">
    {title && <h3>{title}</h3>}
    <p className="records-empty">{!status ? "No supported records are known at your reading progress yet." : status.extraction_status === "ready" ? "No records in this chapter." : status.extraction_status === "failed" ? "Record extraction failed; retry is available in Book settings." : "Knowledge is still being extracted…"}</p>
  </section>;
  return <section className="record-list" aria-label="Chapter records">
    {title && <h3>{title}</h3>}
    {rows.map(row => <article className="record-card" key={row.id}>
      <header><strong>{label(row.type)}</strong>{showChapter && <span>Added to knowledge in chapter {row.source_chapter}</span>}{row.valid_from_chapter != null && <span>Story time: chapter {row.valid_from_chapter}</span>}{row.temporal_qualifier && <em>{temporalLabel(row.temporal_qualifier)}</em>}</header>
      {row.values.map(value => <p key={value.field}><b>{label(value.field)}:</b> {value.rendered ? <><span>{value.rendered}</span> <small>(source: {value.source})</small></> : <><span>{value.source}</span>{value.render_status === "failed" && <small> (source language; English rendering failed)</small>}</>}</p>)}
      {row.polarity && <p><b>Polarity:</b> {row.polarity}</p>}
      {row.attribution && <p><b>Attributed to:</b> {row.attribution}</p>}
      {row.source_value && <p><b>Source assertion:</b> {row.source_value}</p>}
      {row.condition && <p><b>Condition:</b> {row.condition}</p>}
      {row.relation && <p><b>Relation:</b> {row.relation}</p>}
      {row.action && <p><b>Action:</b> {row.action}</p>}
      {row.arguments !== undefined && <p><b>Arguments:</b> {formatNative(row.arguments)}</p>}
      {row.conditions !== undefined && <p><b>Conditions:</b> {formatNative(row.conditions)}</p>}
      {row.literal_arguments !== undefined && <p><b>Arguments:</b> {formatNative(row.literal_arguments)}</p>}
      {!!row.unresolved_references?.length && <p className="record-unresolved"><b>Unresolved references:</b> {row.unresolved_references.join(", ")}</p>}
      {!!row.participants.length && <p className="record-participants"><b>Participants:</b> {row.participants.map((person, i) => <span key={`${person.ordinal ?? i}-${person.surface}`}>{i > 0 && ", "}{person.entity_id && onEntity ? <button type="button" onClick={() => onEntity(person.entity_id!, person.surface)}>{person.surface}</button> : person.entity_id ? <span>{person.surface}</span> : <span className="record-unresolved" title={person.unresolved_reason ?? "Identity unresolved"}>{person.surface} <small>(unresolved)</small></span>}</span>)}</p>}
      {!!row.evidence.length && <details><summary>Source evidence ({row.evidence.length})</summary>{row.evidence.map(e => <blockquote key={`${e.run_id ?? ""}:${e.passage_id}`}>{e.quote || e.text || "Passage evidence"} {e.quote && e.text && e.quote !== e.text && <small> · full passage: {e.text}</small>} <small>chapter {e.chapter}{e.ordinal !== undefined ? ` · passage ${e.ordinal + 1}` : ""}</small></blockquote>)}</details>}
    </article>)}
  </section>;
}

function formatNative(value: unknown): string {
  if (typeof value === "string") return value;
  if (Array.isArray(value) && value.every(item => item && typeof item === "object")) {
    return value.map(item => {
      const entry = item as Record<string, unknown>;
      const role = entry.role ?? entry.field;
      const text = entry.value ?? entry.surface ?? entry.entity_id;
      return role != null && text != null ? `${String(role)}: ${String(text)}` : JSON.stringify(item);
    }).join(", ");
  }
  try { return JSON.stringify(value); } catch { return String(value); }
}

function temporalLabel(value: string): string {
  const normalized = value.toLowerCase().replaceAll("_", " ");
  return normalized.includes("earlier") ? "Recounted from earlier" : normalized;
}
