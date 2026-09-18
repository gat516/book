import { approveCharacterName, confirmGlossaryTerm, correctGlossaryTerm } from "./api";
import type { CharacterNameCandidate, TermRenderingView, TermRole } from "./types";

/**
 * Save a spelling for a source term, whichever state it is in: a pending name is
 * approved, an unconfirmed term is confirmed at the reader's chapter, and a confirmed
 * one is corrected. Shared by the hover card and the chapter's names list so both paths
 * write the same glossary decision. Returns the rendering as it now stands.
 */
export async function saveRendering(novelId: string, rendering: TermRenderingView, targetTerm: string, at: number): Promise<TermRenderingView> {
  if (rendering.status === "pending") {
    const candidate = rendering.candidates.find((item) => item.target_term === targetTerm);
    await approveCharacterName(novelId, rendering.source_term, targetTerm, roleForCandidate(candidate, rendering.term_role));
  } else if (rendering.status === "unlocked") {
    await confirmGlossaryTerm(novelId, {
      source_term: rendering.source_term,
      target_term: targetTerm,
      at_chapter: at,
      term_role: rendering.term_role || "semantic_term",
    });
  } else {
    await correctGlossaryTerm(novelId, rendering.source_term, { target_term: targetTerm, at_chapter: at });
  }
  return { ...rendering, target_term: targetTerm, status: "locked" };
}

export function roleForCandidate(candidate: CharacterNameCandidate | undefined, fallback: TermRenderingView["term_role"]): TermRole {
  if (candidate?.method === "restored_name") return "foreign_person";
  if (candidate?.method === "translated_title") return "personal_title";
  if (candidate?.method === "semantic_translation") return "semantic_term";
  if (candidate?.method === "pinyin") return "chinese_person";
  return fallback || "chinese_person";
}
