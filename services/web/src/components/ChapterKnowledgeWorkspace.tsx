import { useCallback, useEffect, useState } from "react";
import { getRecords, getRecordsInspector, getRecordReviews, retryRecords } from "../api";
import type { RecordReviewResponse, RecordsInspectorResponse, RecordsResponse } from "../types";
import { RecordList } from "./RecordList";
import { ChapterTermsReview } from "./ChapterTermsReview";
import { NameReviewPanel } from "./NameReviewPanel";
import { recordPollInterval, recordsTerminal } from "../recordPolling";
import { usePolling } from "../usePolling";
import { RecordStatusBanner } from "./RecordStatusBanner";
import { RecordReviewPanel } from "./RecordReviewPanel";

/**
 * A read-only extraction inspector. Record publication is immutable; the only action
 * here retries this chapter's failed run. Re-rendering published records needs a new
 * generation for the whole book, so that lives in the book's graph controls (Advanced),
 * not in one chapter's panel. Glossary and name decisions remain in their dedicated views.
 */
export function ChapterKnowledgeWorkspace({ novelId, chapter, at, renderings = [] }: { novelId: string; chapter: number; at: number; renderings?: import("../types").TermRenderingView[] }) {
  const [records, setRecords] = useState<RecordsResponse | null>(null);
  const [inspector, setInspector] = useState<RecordsInspectorResponse | null>(null);
  const [reviews, setReviews] = useState<RecordReviewResponse | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState<"retry" | null>(null);

  const refresh = useCallback(async () => {
    setError("");
    try {
      const [nextRecords, nextInspector, nextReviews] = await Promise.all([
        getRecords(novelId, chapter),
        getRecordsInspector(novelId, chapter),
        getRecordReviews(novelId, chapter),
      ]);
      setRecords(nextRecords);
      setInspector(nextInspector);
      setReviews(nextReviews);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }, [novelId, chapter]);

  useEffect(() => { void refresh(); }, [refresh]);

  usePolling(refresh, recordPollInterval(records), records !== null && !error && !recordsTerminal(records));

  async function retry() {
    setBusy("retry");
    setError("");
    setNotice("");
    try {
      await retryRecords(novelId, chapter);
      setNotice("Extraction retry queued. This panel will update when the worker reports progress.");
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
    {error && <p role="alert" className="reader-pane-error">{error} <button type="button" onClick={() => void refresh()}>Retry diagnostics</button></p>}
    {notice && <p role="status">{notice}</p>}
    {!records || !inspector ? <p role="status">Loading record diagnostics…</p> : <>
      <dl className="records-inspector-summary">
        <div><dt>Parsed</dt><dd>{inspector.parsed}</dd></div>
        <div><dt>Retained</dt><dd>{inspector.retained}</dd></div>
        <div><dt>Dropped</dt><dd>{inspector.dropped}</dd></div>
        <div><dt>Unresolved references</dt><dd>{inspector.unresolved}</dd></div>
        <div><dt>Rendering failures</dt><dd>{inspector.rendering_failures}</dd></div>
      </dl>
      {inspector.rendering_failures > 0 && <p className="glossary-note">
        {inspector.rendering_failures} record{inspector.rendering_failures === 1 ? "" : "s"} could not be rendered into English; the source records are still shown. Re-rendering rebuilds the whole book's graph: Knowledge graph › Advanced › Rebuild graph from scratch.
      </p>}
      {inspector.drops.length > 0 && <details><summary>Dropped records ({inspector.drops.length})</summary><ul className="chapter-knowledge-list">{inspector.drops.map(drop => <li key={drop.original_index}><strong>Record {drop.original_index}</strong><small>{drop.reasons.join("; ")}</small></li>)}</ul></details>}
      {inspector.dropped > 0 && status?.extraction_status !== "failed" && <p className="glossary-note">Dropped records are immutable diagnostics. A new generation is required to change extraction checks; nothing was discarded from the published history.</p>}
      <RecordList rows={records.rows} status={records.status} title="Facts and records" />
      <RecordStatusBanner status={records.status} busy={busy === "retry"} onRetry={() => void retry()} />
      {reviews && <RecordReviewPanel novelId={novelId} chapter={chapter} items={reviews.items} onReviewed={refresh} />}
      <NameReviewPanel novelId={novelId} chapter={chapter} onApproved={() => void refresh()} />
      <ChapterTermsReview novelId={novelId} at={at} renderings={renderings} />
    </>}
  </section>;
}
