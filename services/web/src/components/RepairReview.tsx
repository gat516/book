import { useCallback, useEffect, useMemo, useState } from "react";
import { getRepairPreview, requestRepair } from "../api";
import { readerId } from "../readerId";
import type {
  RepairPreview,
  RepairReportClaim,
  RepairReportMention,
  RepairTrackName,
} from "../types";

interface Props {
  novelId: string;
  track: RepairTrackName;
  onSubmitted: () => void;
  onClose: () => void;
}

type MentionVerdict = { correct: boolean; unambiguous: boolean };

/**
 * Review the staged claims against their source quotes.
 *
 * This component collects booleans and nothing else. It computes no scores and asserts no
 * aggregates, because record_review deliberately derives every metric itself from these
 * per-item assessments joined against the bindings actually stored — trusting a
 * caller-supplied "precision: 0.99" would make the activation gate decorative.
 *
 * Partial work is kept in sessionStorage keyed by the report hash, so a long review
 * survives a reload but is discarded the moment the underlying report changes (which
 * happens whenever a rebuild publishes another chapter).
 */
export function RepairReview({ novelId, track, onSubmitted, onClose }: Props) {
  const [preview, setPreview] = useState<RepairPreview | null>(null);
  const [mentions, setMentions] = useState<Record<string, MentionVerdict>>({});
  const [facts, setFacts] = useState<Record<number, boolean>>({});
  const [reviewer, setReviewer] = useState("");
  const [regressions, setRegressions] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const report = preview?.report ?? null;
  const reviewHash = report?.review_hash ?? "";
  const storageKey = reviewHash ? `repair-review-${novelId}-${track}-${reviewHash}` : "";

  const load = useCallback(async () => {
    setError(null);
    const next = await getRepairPreview(novelId, track);
    setPreview(next);
  }, [novelId, track]);

  useEffect(() => {
    void load().catch((err) => setError(String(err)));
  }, [load]);

  // Restore a partially finished review, but only for this exact report.
  useEffect(() => {
    if (!storageKey) return;
    try {
      const saved = sessionStorage.getItem(storageKey);
      if (!saved) return;
      const parsed = JSON.parse(saved);
      setMentions(parsed.mentions ?? {});
      setFacts(parsed.facts ?? {});
      setReviewer(parsed.reviewer ?? "");
      setRegressions(parsed.regressions ?? 0);
    } catch {
      // A corrupt or unreadable draft is not worth surfacing: start clean.
    }
  }, [storageKey]);

  useEffect(() => {
    if (!storageKey) return;
    try {
      sessionStorage.setItem(
        storageKey,
        JSON.stringify({ mentions, facts, reviewer, regressions }),
      );
    } catch {
      // Private-mode browsers can refuse. The review still works, it just won't resume.
    }
  }, [storageKey, mentions, facts, reviewer, regressions]);

  const reportMentions: RepairReportMention[] = useMemo(
    () => (Array.isArray(report?.mentions) ? report.mentions : []),
    [report],
  );
  const reportClaims: RepairReportClaim[] = useMemo(
    () => (Array.isArray(report?.claims) ? report.claims : []),
    [report],
  );

  const assessedMentions = Object.keys(mentions).length;
  const assessedFacts = Object.keys(facts).length;
  const allMentionsAssessed = reportMentions.length > 0 && assessedMentions === reportMentions.length;
  const allFactsAssessed = reportClaims.length > 0 && assessedFacts === reportClaims.length;
  const exhaustiveGraphReview = track !== "graph" || (allMentionsAssessed && allFactsAssessed);

  async function submit() {
    if (!reviewHash || !reviewer.trim()) return;
    setSaving(true);
    setError(null);
    try {
      await requestRepair(novelId, {
        track,
        action: "review",
        revision_id: preview?.revision_id,
        params: {
          document: {
            review_hash: reviewHash,
            reviewer: reviewer.trim(),
            approved: true,
            known_merge_regressions: regressions,
            mentions: Object.entries(mentions).map(([id, verdict]) => ({
              id,
              correct: verdict.correct,
              unambiguous: verdict.unambiguous,
            })),
            facts: Object.entries(facts).map(([id, correct]) => ({
              id: Number(id),
              correct,
            })),
          },
        },
      });
      try {
        sessionStorage.removeItem(storageKey);
      } catch {
        // Nothing to do; the draft is keyed by report hash and will be ignored anyway.
      }
      onSubmitted();
    } catch (err) {
      setError(String(err));
    } finally {
      setSaving(false);
    }
  }

  if (error && !preview) {
    return (
      <section className="repair-review" aria-label="Review rebuilt knowledge">
        <p className="chapter-list-error" role="alert">
          {error}
        </p>
        <button type="button" onClick={onClose}>
          Close
        </button>
      </section>
    );
  }

  if (!report) {
    return (
      <section className="repair-review" aria-label="Review rebuilt knowledge">
        <p role="status">
          No review report yet. A report is a frozen snapshot used for the activation
          gate: <code>record_review</code> matches a hash against it, and publishing a
          chapter clears it — so one taken mid-rebuild would be invalidated by the very
          next chapter. It is written once every chapter has finished.
        </p>
        <p className="novel-create-form-hint">
          To watch progress meanwhile, close this and open the chapter's <strong>Chapter
          knowledge</strong> panel in the reader — its Activity feed lists terms and facts
          as each model call completes, without waiting for a chapter.
        </p>
        <button type="button" onClick={onClose}>
          Close
        </button>
      </section>
    );
  }

  return (
    <section className="repair-review" aria-label="Review rebuilt knowledge">
      <header className="repair-review-header">
        <h3>Review rebuilt knowledge</h3>
        <button type="button" onClick={onClose}>
          Close
        </button>
      </header>

      <p className="novel-create-form-hint">
        Assess each proposed fact, then each identity link, against its translated context. Scores are computed
        by the server from these answers and the stored bindings — this screen never
        decides whether the rebuild qualifies.
      </p>

      <dl className="repair-review-progress">
        <div>
          <dt>Identities assessed</dt>
          <dd>{assessedMentions} / {reportMentions.length}</dd>
        </div>
        <div>
          <dt>Claims assessed</dt>
          <dd>
            {assessedFacts} / {reportClaims.length} published
          </dd>
        </div>
        <div>
          <dt>Evidence re-checked</dt>
          <dd>{report.evidence_valid ? "valid" : "invalid"}</dd>
        </div>
      </dl>

      <h4>Proposed facts ({reportClaims.length})</h4>
      {reportClaims.length === 0 && <p role="status">No facts were published into this rebuild.</p>}
      <ul className="repair-review-list">
        {reportClaims.map((claim) => (
          <li key={claim.id}>
            <p className="repair-review-surface">
              <strong>Proposed fact:</strong> {claim.entity ?? "Unknown subject"} — {claim.attribute}: {claim.value}
              {claim.chapter !== undefined && <small> · chapter {claim.chapter}</small>}
            </p>
            <p><strong>Translated evidence</strong></p>
            {(claim.target_context ?? claim.quote) && <blockquote>{claim.target_context ?? claim.quote}</blockquote>}
            {claim.target_context && <details>
              <summary>Source-language audit record</summary>
              <p>{claim.entity_source ?? claim.entity}</p>
              {claim.quote && <blockquote>{claim.quote}</blockquote>}
            </details>}
            <fieldset>
              <legend>Does the translated evidence support this proposed fact?</legend>
              <label>
                <input type="radio" name={`fact-${claim.id}`}
                  checked={facts[claim.id] === true}
                  onChange={() => setFacts((current) => ({ ...current, [claim.id]: true }))} />{" "}
                Supported
              </label>
              <label>
                <input type="radio" name={`fact-${claim.id}`}
                  checked={facts[claim.id] === false}
                  onChange={() => setFacts((current) => ({ ...current, [claim.id]: false }))} />{" "}
                Not supported
              </label>
            </fieldset>
          </li>
        ))}
      </ul>

      <h4>Identity links ({reportMentions.length})</h4>
      <p className="novel-create-form-hint">These are term-to-entity checks used to prevent two characters or concepts from being merged. They are separate from the proposed facts above.</p>
      {reportMentions.length === 0 && <p role="status">No identities to review.</p>}
      <ul className="repair-review-list">
        {reportMentions.map((mention) => {
          const verdict = mentions[mention.id];
          return (
            <li key={mention.id}>
              <p className="repair-review-surface">
                <strong>Identity decision:</strong>{" "}
                {mention.surface_target ?? "Target term unavailable"} → {mention.entity ?? "Unlinked"}
                {mention.chapter !== undefined && <small> · chapter {mention.chapter}</small>}
              </p>
              <p><strong>Translated context</strong></p>
              {(mention.target_context ?? mention.quote) && <blockquote>{mention.target_context ?? mention.quote}</blockquote>}
              {mention.target_context && <details>
                <summary>Source-language audit record</summary>
                <p>{mention.surface}{mention.entity ? <> → {mention.entity}</> : <> → unlinked</>}</p>
                {mention.quote && <blockquote>{mention.quote}</blockquote>}
              </details>}
              <label>
                <input
                  type="checkbox"
                  checked={verdict?.correct ?? false}
                  onChange={(event) =>
                    setMentions((current) => ({
                      ...current,
                      [mention.id]: {
                        correct: event.target.checked,
                        unambiguous: current[mention.id]?.unambiguous ?? false,
                      },
                    }))
                  }
                />{" "}
                Link is correct
              </label>
              <label>
                <input
                  type="checkbox"
                  checked={verdict?.unambiguous ?? false}
                  onChange={(event) =>
                    setMentions((current) => ({
                      ...current,
                      [mention.id]: {
                        correct: current[mention.id]?.correct ?? false,
                        unambiguous: event.target.checked,
                      },
                    }))
                  }
                />{" "}
                Source is unambiguous
              </label>
            </li>
          );
        })}
      </ul>

      <div className="repair-review-submit">
        <label>
          Reviewer
          <input
            value={reviewer}
            onChange={(event) => setReviewer(event.target.value)}
            placeholder={readerId().slice(0, 8)}
          />
        </label>
        <label>
          Known merge regressions
          <input
            type="number"
            min={0}
            value={regressions}
            onChange={(event) => setRegressions(Math.max(0, Number(event.target.value)))}
          />
        </label>
        <button type="button" onClick={() => void submit()}
          disabled={saving || !reviewer.trim() || !exhaustiveGraphReview}>
          {saving ? "Submitting…" : "Submit review"}
        </button>
        {track === "graph" && !exhaustiveGraphReview && (
          <p className="novel-create-form-hint">
            Every source identity and published claim must be assessed. Small revisions
            can activate once everything present is reviewed and passes the accuracy gate.
          </p>
        )}
      </div>

      {error && (
        <p className="chapter-list-error" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}
