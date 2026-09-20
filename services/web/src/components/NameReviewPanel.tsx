import { useCallback, useEffect, useRef, useState } from "react";
import { approveCharacterName, getCharacterNameReviews } from "../api";
import type { CharacterNameReview, TermRole } from "../types";

interface Props {
  novelId: string;
  chapter?: number;
  filter?: string;
  onApproved?: () => void;
  onCount?: (count: number) => void;
}

const PAGE = 60;

const ROLES: [TermRole, string][] = [
  ["chinese_person", "Chinese personal name — use pinyin"],
  ["foreign_person", "Foreign or transcribed name — restore spelling"],
  ["personal_title", "Personal title or epithet — translate meaning"],
  ["semantic_term", "Place, group, object, or technique — translate meaning"],
];

/**
 * The spellings waiting for a decision, one row each. A row carries the provisional
 * spelling and confirms it in one click; opening a row is what brings up the source
 * quote, the term type and a correction field. A book can hold hundreds of these, so
 * nothing but the open row renders a control.
 */
export function NameReviewPanel({ novelId, chapter, filter = "", onApproved, onCount }: Props) {
  const [reviews, setReviews] = useState<CharacterNameReview[]>([]);
  const [open, setOpen] = useState<string | null>(null);
  const [custom, setCustom] = useState<Record<string, string>>({});
  const [roles, setRoles] = useState<Record<string, TermRole>>({});
  const [saving, setSaving] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [shown, setShown] = useState(PAGE);
  // Bulk confirm: how many of `total` are done. Confirming a spelling that is already in
  // the text queues no chapter work (ingest-api respellFor), so this only writes terms.
  const [bulk, setBulk] = useState<{ done: number; total: number } | null>(null);
  const stopped = useRef(false);

  const load = useCallback(async () => {
    const response = await getCharacterNameReviews(novelId, chapter);
    setReviews(response.reviews);
    setRoles((current) => {
      const next = { ...current };
      for (const review of response.reviews) if (!next[review.source_term]) next[review.source_term] = review.term_role;
      return next;
    });
  }, [novelId, chapter]);

  useEffect(() => {
    setCustom({});
    setRoles({});
    setOpen(null);
    void load().catch((err) => setError(String(err)));
  }, [load]);

  useEffect(() => { onCount?.(reviews.length); }, [reviews.length, onCount]);
  useEffect(() => { setShown(PAGE); }, [filter]);

  async function approve(review: CharacterNameReview, target: string) {
    if (!target.trim()) return;
    setSaving(review.source_term);
    setError(null);
    try {
      await approveCharacterName(novelId, review.source_term, target.trim(), roles[review.source_term] || review.term_role);
      setOpen(null);
      await load();
      onApproved?.();
    } catch (err) {
      setError(String(err));
    } finally {
      setSaving(null);
    }
  }

  async function confirmAll(list: CharacterNameReview[]) {
    stopped.current = false;
    setError(null);
    setOpen(null);
    setBulk({ done: 0, total: list.length });
    let done = 0;
    for (const review of list) {
      if (stopped.current) break;
      const target = review.candidates[0]?.target_term ?? "";
      try {
        await approveCharacterName(novelId, review.source_term, target, roles[review.source_term] || review.term_role);
        setBulk({ done: ++done, total: list.length });
      } catch (err) {
        setError(`${err} — stopped after ${done} of ${list.length}.`);
        break;
      }
    }
    setBulk(null);
    await load().catch((err) => setError(String(err)));
    if (done) onApproved?.();
  }

  const needle = filter.trim().toLowerCase();
  const matching = needle
    ? reviews.filter((review) => review.source_term.toLowerCase().includes(needle) ||
        (review.candidates[0]?.target_term ?? "").toLowerCase().includes(needle))
    : reviews;
  const confirmable = matching.filter((review) => review.candidates[0]?.target_term);
  if (!reviews.length && !error) return null;

  return <section className="term-queue" aria-label="Spellings to review">
    <div className="term-head">
      <h3>To review</h3>
      <span className="term-count">{matching.length === reviews.length
        ? `${reviews.length}`
        : `${matching.length} of ${reviews.length}`}</span>
      {bulk
        ? <span className="term-bulk">
            <span role="status">Confirming {bulk.done} of {bulk.total}…</span>
            <button type="button" onClick={() => { stopped.current = true; }}>Stop</button>
          </span>
        : confirmable.length > 1 && <button type="button" className="term-bulk-start" disabled={!!saving}
            onClick={() => void confirmAll(confirmable)}>
            Confirm {confirmable.length === reviews.length ? "all" : `these ${confirmable.length}`}
          </button>}
    </div>
    <p className="term-hint">Each term already reads with its provisional spelling. Confirming keeps it for future chapters and leaves saved chapters as they are; changing one swaps the spelling through the text you have.</p>
    {error && <p role="alert" className="term-error">{error}</p>}
    {!matching.length ? <p className="term-empty">No term matches “{filter}”.</p> : <>
      <ul className="term-list">
        {matching.slice(0, shown).map((review) => {
          const provisional = review.candidates[0]?.target_term ?? "";
          const isOpen = open === review.source_term;
          const busy = saving === review.source_term;
          return <li key={review.source_term} className={`term-row${isOpen ? " is-open" : ""}`}>
            <div className="term-line">
              <span className="term-source" lang="zh">{review.source_term}</span>
              <span className="term-target">{provisional || <em>no spelling yet</em>}</span>
              <span className="term-actions">
                <button type="button" className="term-confirm" disabled={busy || !provisional}
                  onClick={() => void approve(review, provisional)}>{busy ? "Saving…" : "Confirm"}</button>
                <button type="button" className="term-open" aria-expanded={isOpen}
                  onClick={() => setOpen(isOpen ? null : review.source_term)}>Change</button>
              </span>
            </div>
            {isOpen && <div className="term-editor">
              <blockquote lang="zh">{review.quote}</blockquote>
              <label>Term type
                <select value={roles[review.source_term] || review.term_role} disabled={busy}
                  onChange={(event) => setRoles((old) => ({ ...old, [review.source_term]: event.target.value as TermRole }))}>
                  {ROLES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                </select>
              </label>
              <form onSubmit={(event) => {
                event.preventDefault();
                void approve(review, custom[review.source_term] ?? provisional);
              }}>
                <label>Spelling
                  <input value={custom[review.source_term] ?? provisional} disabled={busy} autoFocus
                    aria-label={`Spelling for ${review.source_term}`}
                    onChange={(event) => setCustom((old) => ({ ...old, [review.source_term]: event.target.value }))} />
                </label>
                <div className="term-editor-actions">
                  <button disabled={busy || !(custom[review.source_term] ?? provisional).trim()}>Save spelling</button>
                  <button type="button" disabled={busy} onClick={() => setOpen(null)}>Cancel</button>
                </div>
              </form>
            </div>}
          </li>;
        })}
      </ul>
      {matching.length > shown && <button type="button" className="term-more" onClick={() => setShown((n) => n + PAGE)}>
        Show {Math.min(PAGE, matching.length - shown)} more
      </button>}
    </>}
  </section>;
}
