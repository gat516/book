import { useRef, useState } from "react";
import { patchRecordReview } from "../api";
import type { RecordReviewItem } from "../types";
import { RecordList } from "./RecordList";
import { requestIDForIntent, reviewIntentKey, type ReviewDecision } from "../recordReview";

export function RecordReviewPanel({ novelId, chapter, items, onReviewed }: {
  novelId: string;
  chapter: number;
  items: RecordReviewItem[];
  onReviewed: () => Promise<void> | void;
}) {
  const [reasons, setReasons] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const requestIDs = useRef(new Map<string, string>());

  async function decide(item: RecordReviewItem, decision: ReviewDecision) {
    const reason = (reasons[item.row.id] ?? "").trim();
    if (!reason) {
      setError("Add a reason before accepting or rejecting a record.");
      return;
    }
    setBusy(`${item.row.id}:${decision}`);
    setError(null);
    // Keep the key when PATCH fails: a transport-error re-click must be an idempotent
    // retry of the same intent, not a second append-only decision.
    const intent = reviewIntentKey(item.row.id, decision, reason);
    const body = { row_id: item.row.id, decision, reason, request_id: requestIDForIntent(requestIDs.current, intent) } as const;
    try {
      await patchRecordReview(novelId, chapter, body);
      await onReviewed();
      requestIDs.current.delete(intent);
      setReasons(current => ({ ...current, [item.row.id]: "" }));
    } catch (reasonValue) {
      setError(reasonValue instanceof Error ? reasonValue.message : String(reasonValue));
    } finally {
      setBusy(null);
    }
  }

  return <section className="record-review-panel" aria-labelledby="record-review-heading">
    <h3 id="record-review-heading">Review records</h3>
    <p className="glossary-note">Each decision is append-only. Rejected records stay here for audit history, but are not shown in the reader’s normal Facts and records list.</p>
    {error && <p role="alert" className="reader-pane-error">{error}</p>}
    {!items.length && <p>No records are available for review yet.</p>}
    {items.map(item => {
      const decision = item.decision;
      const key = item.row.id;
      return <article className={`record-review-item record-review-${decision?.decision ?? "unreviewed"}`} key={key}>
        <RecordList rows={[item.row]} title={decision?.decision === "rejected" ? "Rejected record (review history)" : undefined} />
        {decision && <p className="record-review-decision"><strong>Latest decision: {decision.decision}</strong> by {decision.actor} on {new Date(decision.created_at).toLocaleString()}. Reason: {decision.reason}</p>}
        <label>Decision reason (required)
          <textarea value={reasons[key] ?? ""} onChange={event => setReasons(current => ({ ...current, [key]: event.target.value }))} placeholder="Why should this record be kept or rejected?" />
        </label>
        <div className="record-review-actions">
          <button type="button" disabled={busy !== null || !(reasons[key] ?? "").trim()} onClick={() => void decide(item, "accepted")}>{busy === `${key}:accepted` ? "Saving…" : "Accept"}</button>
          <button type="button" disabled={busy !== null || !(reasons[key] ?? "").trim()} onClick={() => void decide(item, "rejected")}>{busy === `${key}:rejected` ? "Saving…" : "Reject"}</button>
        </div>
      </article>;
    })}
  </section>;
}
