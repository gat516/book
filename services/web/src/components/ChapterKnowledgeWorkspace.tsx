import { notifyKnowledgeUpdated, useKnowledgeRevision } from "../knowledgeUpdates";
import { useCallback, useEffect, useState } from "react";
import { discardRecordsChapter, getRecords, getRecordsInspector, getRecordReviews, retryRecords } from "../api";
import type { RecordReviewResponse, RecordsInspectorResponse, RecordsResponse } from "../types";
import { RecordList } from "./RecordList";
import { ChapterTermsReview } from "./ChapterTermsReview";
import { NameReviewPanel } from "./NameReviewPanel";
import { recordPollInterval, recordsTerminal } from "../recordPolling";
import { usePolling } from "../usePolling";
import { RecordStatusBanner } from "./RecordStatusBanner";
import { RecordReviewPanel } from "./RecordReviewPanel";

// Chapter extraction controls and optional detailed knowledge/term review.
export function ChapterKnowledgeWorkspace({ novelId, chapter, at, renderings = [] }: { novelId: string; chapter: number; at: number; renderings?: import("../types").TermRenderingView[] }) {
  const revision = useKnowledgeRevision(novelId);
  const [records, setRecords] = useState<RecordsResponse | null>(null);
  const [inspector, setInspector] = useState<RecordsInspectorResponse | null>(null);
  const [reviews, setReviews] = useState<RecordReviewResponse | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState<"retry" | "stop" | null>(null);

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

  useEffect(() => { void refresh(); }, [refresh, revision]);

  usePolling(refresh, recordPollInterval(records), records !== null && !error && !recordsTerminal(records));

  async function retry() {
    setBusy("retry");
    setError("");
    setNotice("");
    try {
      await retryRecords(novelId, chapter);
      setNotice("Extraction retry queued. This panel will update when the worker reports progress.");
      notifyKnowledgeUpdated(novelId);
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(null);
    }
  }

  async function stopChapter() {
    setBusy("stop"); setError(""); setNotice("");
    try {
      await discardRecordsChapter(novelId, chapter);
      setNotice("Chapter extraction paused. Use Extract facts for this chapter to resume it.");
      notifyKnowledgeUpdated(novelId);
      await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setBusy(null); }
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
        <div><dt>Discovered candidates</dt><dd>{inspector.counts?.discovered ?? inspector.parsed}</dd></div>
        <div><dt>Selected claims</dt><dd>{inspector.counts?.selected ?? inspector.retained}</dd></div>
        <div><dt>Omitted</dt><dd>{inspector.counts?.omitted ?? 0}</dd></div>
        <div><dt>Consolidated</dt><dd>{inspector.counts?.consolidated ?? 0}</dd></div>
        <div><dt>Rejected assertions</dt><dd>{inspector.counts?.rejected ?? inspector.dropped}</dd></div>
        <div><dt>Unrepresented</dt><dd>{inspector.counts?.unrepresented ?? inspector.unresolved}</dd></div>
        <div><dt>Published outputs</dt><dd>{inspector.counts?.published ?? inspector.retained}</dd></div>
        <div><dt>Rendering failures</dt><dd>{inspector.rendering_failures}</dd></div>
      </dl>
      {inspector.selection_outcome === "all_rejected" && <p className="glossary-note">All selected assertions were rejected during validation.</p>}
      {inspector.selection_outcome === "empty" && <p className="glossary-note">{(inspector.counts?.discovered ?? inspector.parsed) > 0 ? "Selection intentionally retained no claims." : "No candidates were discovered for this chapter."}</p>}
      {inspector.stages && <p className="glossary-note">Stages: {Object.entries(inspector.stages).map(([name, value]) => `${name}: ${value}`).join(" · ")}</p>}
      {inspector.rendering_failures > 0 && <p className="glossary-note">
        {inspector.rendering_failures} record{inspector.rendering_failures === 1 ? "" : "s"} could not be rendered into English; the source records are still shown. To replace these renderings, use Book knowledge → Replace existing facts → Re-extract all chapters. This replaces knowledge for the whole book.
      </p>}
      <div className="knowledge-actions">
        {(status?.extraction_status === "pending" || status?.extraction_status === "failed") && <button type="button" disabled={busy !== null} onClick={() => void retry()}>{busy === "retry" ? "Queuing…" : status.extraction_status === "failed" ? "Retry chapter extraction" : "Extract facts for this chapter"}</button>}
        {status?.extraction_status === "processing" && <button type="button" disabled={busy !== null} onClick={() => void stopChapter()}>{busy === "stop" ? "Pausing…" : "Pause chapter extraction"}</button>}
      </div>
      {inspector.drops.length > 0 && <details><summary>Dropped records ({inspector.drops.length})</summary><ul className="chapter-knowledge-list">{inspector.drops.map(drop => <li key={drop.original_index}><strong>Record {drop.original_index}</strong><small>{drop.reasons.join("; ")}</small></li>)}</ul></details>}
      {inspector.dropped > 0 && status?.extraction_status !== "failed" && <p className="glossary-note">Dropped records are immutable diagnostics. A new generation is required to change extraction checks; nothing was discarded from the published history.</p>}
      <RecordList rows={records.rows} status={records.status} title="Facts, relationships, and events" />
      <RecordStatusBanner status={records.status} busy={busy === "retry"} onRetry={() => void retry()} />
      {reviews && <RecordReviewPanel novelId={novelId} chapter={chapter} items={reviews.items} onReviewed={refresh} />}
      <NameReviewPanel novelId={novelId} chapter={chapter} onApproved={() => void refresh()} />
      <ChapterTermsReview novelId={novelId} at={at} renderings={renderings} />
    </>}
  </section>;
}
