import { useCallback, useEffect, useState } from "react";
import { approveCharacterName, getCharacterNameReviews } from "../api";
import type { CharacterNameReview } from "../types";

interface Props {
  novelId: string;
  chapter?: number;
  onApproved?: () => void;
}

export function NameReviewPanel({ novelId, chapter, onApproved }: Props) {
  const [reviews, setReviews] = useState<CharacterNameReview[]>([]);
  const [choices, setChoices] = useState<Record<string, string>>({});
  const [custom, setCustom] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    const response = await getCharacterNameReviews(novelId, chapter);
    setReviews(response.reviews);
    setChoices((current) => {
      const next = { ...current };
      for (const review of response.reviews) {
        if (!next[review.source_term] && review.candidates[0]) next[review.source_term] = review.candidates[0].target_term;
      }
      return next;
    });
  }, [novelId, chapter]);

  useEffect(() => {
    void load().catch((err) => setError(String(err)));
  }, [load]);

  async function approve(review: CharacterNameReview) {
    const target = (custom[review.source_term] || choices[review.source_term] || "").trim();
    if (!target) return;
    setSaving(review.source_term);
    setError(null);
    try {
      await approveCharacterName(novelId, review.source_term, target);
      await load();
      onApproved?.();
    } catch (err) {
      setError(String(err));
    } finally {
      setSaving(null);
    }
  }

  if (!reviews.length && !error) return null;
  return <section className="name-review" aria-label="Character name spelling review">
    <h3>Character name spelling required</h3>
    <p className="glossary-note">Approve the spelling used in translation. This controls terminology only; it does not merge identities or approve character facts.</p>
    {error && <p role="alert" className="chapter-pending-error">{error}</p>}
    {reviews.map((review) => <fieldset key={review.source_term} disabled={saving === review.source_term}>
      <legend><span lang="zh">{review.source_term}</span> · {review.reason.replaceAll("_", " ")}</legend>
      <blockquote lang="zh">{review.quote}</blockquote>
      {review.candidates.map((candidate) => <label key={candidate.target_term} className="name-review-choice">
        <input type="radio" name={`name-${review.source_term}`} value={candidate.target_term}
          checked={!custom[review.source_term] && choices[review.source_term] === candidate.target_term}
          onChange={() => { setCustom((old) => ({ ...old, [review.source_term]: "" })); setChoices((old) => ({ ...old, [review.source_term]: candidate.target_term })); }} />
        <strong>{candidate.target_term}</strong>
        <span>{candidate.segmentation} · {candidate.pronunciation.join(" + ")}</span>
      </label>)}
      <label>Custom spelling
        <input value={custom[review.source_term] ?? ""} placeholder="Enter the correct spelling"
          onChange={(event) => setCustom((old) => ({ ...old, [review.source_term]: event.target.value }))} />
      </label>
      <button onClick={() => void approve(review)} disabled={saving === review.source_term || !(custom[review.source_term] || choices[review.source_term])}>
        {saving === review.source_term ? "Approving…" : "Approve spelling and resume"}
      </button>
    </fieldset>)}
  </section>;
}
