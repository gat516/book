import { useCallback, useEffect, useState } from "react";
import { approveCharacterName, getCharacterNameReviews } from "../api";
import type { CharacterNameReview, TermRole } from "../types";

interface Props {
  novelId: string;
  chapter?: number;
  onApproved?: () => void;
}

export function NameReviewPanel({ novelId, chapter, onApproved }: Props) {
  const [reviews, setReviews] = useState<CharacterNameReview[]>([]);
  const [choices, setChoices] = useState<Record<string, string>>({});
  const [custom, setCustom] = useState<Record<string, string>>({});
  const [roles, setRoles] = useState<Record<string, TermRole>>({});
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
    setRoles((current) => {
      const next = { ...current };
      for (const review of response.reviews) if (!next[review.source_term]) next[review.source_term] = review.term_role;
      return next;
    });
  }, [novelId, chapter]);

  useEffect(() => {
    setChoices({});
    setCustom({});
    setRoles({});
    void load().catch((err) => setError(String(err)));
  }, [load]);

  async function approve(review: CharacterNameReview) {
    const target = (custom[review.source_term] || choices[review.source_term] || "").trim();
    if (!target) return;
    setSaving(review.source_term);
    setError(null);
    try {
      await approveCharacterName(novelId, review.source_term, target, roles[review.source_term] || review.term_role);
      await load();
      onApproved?.();
    } catch (err) {
      setError(String(err));
    } finally {
      setSaving(null);
    }
  }

  if (!reviews.length && !error) return null;
  return <section className="name-review" aria-label="Term rendering review">
    <h3>Review term spellings</h3>
    <p className="glossary-note">Each term already has one provisional spelling. Confirm it or enter a correction; review does not block reading.</p>
    {error && <p role="alert" className="chapter-pending-error">{error}</p>}
    {reviews.map((review) => <fieldset key={review.source_term} disabled={saving === review.source_term}>
      <legend><span lang="zh">{review.source_term}</span> · {review.reason.replaceAll("_", " ")}</legend>
      <blockquote lang="zh">{review.quote}</blockquote>
      <label>Term type
        <select value={roles[review.source_term] || review.term_role}
          onChange={(event) => setRoles((old) => ({ ...old, [review.source_term]: event.target.value as TermRole }))}>
          <option value="chinese_person">Chinese personal name — use pinyin</option>
          <option value="foreign_person">Foreign/transcribed name — restore spelling</option>
          <option value="personal_title">Personal title or epithet — translate meaning</option>
          <option value="semantic_term">Species, group, place, organization, object, or technique — translate meaning</option>
        </select>
      </label>
      {review.candidates[0] && <p>
        Using <strong>{review.candidates[0].target_term}</strong> provisionally
        {review.candidates[0].method === "pinyin" ? " · Pinyin" : ""}.
      </p>}
      <label>Correct spelling (optional)
        <input value={custom[review.source_term] ?? ""} placeholder="Enter the correct spelling"
          onChange={(event) => setCustom((old) => ({ ...old, [review.source_term]: event.target.value }))} />
      </label>
      <button onClick={() => void approve(review)} disabled={saving === review.source_term || !(custom[review.source_term] || choices[review.source_term])}>
        {saving === review.source_term ? "Approving…" : "Confirm spelling"}
      </button>
    </fieldset>)}
  </section>;
}
