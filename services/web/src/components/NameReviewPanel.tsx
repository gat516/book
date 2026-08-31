import { useCallback, useEffect, useState } from "react";
import { approveCharacterName, getCharacterNameReviews } from "../api";
import type { CharacterNameCandidate, CharacterNameReview, TermRole } from "../types";

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
    <h3>Term rendering required</h3>
    <p className="glossary-note">Choose both what the source term is and how it should appear in translation. This controls terminology only; it does not merge identities or approve character facts.</p>
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
      {review.candidates.map((candidate) => <label key={candidate.target_term} className="name-review-choice">
        <input type="radio" name={`name-${review.source_term}`} value={candidate.target_term}
          checked={!custom[review.source_term] && choices[review.source_term] === candidate.target_term}
          onChange={() => {
            setCustom((old) => ({ ...old, [review.source_term]: "" }));
            setChoices((old) => ({ ...old, [review.source_term]: candidate.target_term }));
            setRoles((old) => ({ ...old, [review.source_term]: roleForCandidate(candidate, review.term_role) }));
          }} />
        <strong>{candidate.target_term}</strong>
        <span>{candidate.method === "restored_name" ? "Suggested restored name"
          : candidate.method === "translated_title" ? "Suggested translated title"
          : candidate.method === "semantic_translation" ? "Suggested meaning-based translation"
          : `Pinyin · ${candidate.segmentation} · ${candidate.pronunciation.join(" + ")}`}</span>
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

function roleForCandidate(candidate: CharacterNameCandidate, fallback: TermRole): TermRole {
  if (candidate.method === "restored_name") return "foreign_person";
  if (candidate.method === "translated_title") return "personal_title";
  if (candidate.method === "semantic_translation") return "semantic_term";
  if (candidate.method === "pinyin") return "chinese_person";
  return fallback;
}
