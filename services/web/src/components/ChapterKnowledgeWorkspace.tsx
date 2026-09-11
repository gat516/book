import { useCallback, useEffect, useState } from "react";
import { getRecords, getRecordsInspector, retryRecordRendering, retryRecords } from "../api";
import type { RecordsInspectorResponse, RecordsResponse } from "../types";
import { RecordList } from "./RecordList";

/**
 * A read-only extraction inspector. Record publication is immutable; the only actions
 * here schedule a retry of the failed run, or rerun English rendering for already
 * published source records. Glossary and name decisions remain in their dedicated views.
 */
export function ChapterKnowledgeWorkspace({ novelId, chapter, at }: { novelId: string; chapter: number; at: number }) {
  const [records, setRecords] = useState<RecordsResponse | null>(null);
  const [inspector, setInspector] = useState<RecordsInspectorResponse | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<"retry" | "render" | null>(null);

  const refresh = useCallback(async () => {
    setError("");
    try {
      const [nextRecords, nextInspector] = await Promise.all([
        getRecords(novelId, chapter),
        getRecordsInspector(novelId, chapter),
      ]);
      setRecords(nextRecords);
      setInspector(nextInspector);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }, [novelId, chapter]);

  useEffect(() => { void refresh(); }, [refresh]);

  async function run(action: "retry" | "render") {
    setBusy(action);
    setError("");
    try {
      if (action === "retry") await retryRecords(novelId, chapter);
      else await retryRecordRendering(novelId, chapter);
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(null);
    }
  }

  const status = records?.status;
  return <section className="chapter-knowledge" aria-labelledby="chapter-records-heading">
    <header>
      <div>
        <h2 id="chapter-records-heading">Record extraction inspector</h2>
        <p>Structured knowledge learned in chapter {chapter}. The reader gate is chapter {at}.</p>
      </div>
      {status && <span className={`knowledge-badge records-${status.extraction_status}`}>{status.extraction_status}</span>}
    </header>
    {error && <p role="alert" className="reader-pane-error">{error}</p>}
    {!records || !inspector ? <p role="status">Loading record diagnostics…</p> : <>
      <dl className="records-inspector-summary">
        <div><dt>Parsed</dt><dd>{inspector.parsed}</dd></div>
        <div><dt>Retained</dt><dd>{inspector.retained}</dd></div>
        <div><dt>Dropped</dt><dd>{inspector.dropped}</dd></div>
        <div><dt>Unresolved references</dt><dd>{inspector.unresolved}</dd></div>
        <div><dt>Rendering failures</dt><dd>{inspector.rendering_failures}</dd></div>
      </dl>
      <div className="knowledge-actions">
        {(status?.extraction_status === "failed" || inspector.dropped > 0) && <button disabled={busy !== null} onClick={() => void run("retry")}>{busy === "retry" ? "Retrying extraction…" : "Retry extraction"}</button>}
        {inspector.rendering_failures > 0 && <button disabled={busy !== null} onClick={() => void run("render")}>{busy === "render" ? "Retrying rendering…" : "Retry English rendering"}</button>}
      </div>
      {inspector.drops.length > 0 && <details><summary>Dropped records ({inspector.drops.length})</summary><ul className="chapter-knowledge-list">{inspector.drops.map(drop => <li key={drop.original_index}><strong>Record {drop.original_index}</strong><small>{drop.reasons.join("; ")}</small></li>)}</ul></details>}
      <RecordList rows={records.rows} status={records.status} />
    </>}
  </section>;
}
