import type { RecordsRebuildStatus } from "./types";

export function graphCoverageLabel(status: RecordsRebuildStatus): string {
  if (!status.active_generation_id) return "No facts have been extracted for this book.";
  const coverage = `${status.published_chapters}/${status.eligible_chapters} chapters published`;
  return status.has_predecessor
    ? `Replacing book knowledge: ${coverage} (${status.missing_chapters} remaining).`
    : `Extracted knowledge: ${coverage}.`;
}

export function chapterKnowledgeReviewLabel(chapter: number): string {
  return `Review knowledge added in chapter ${chapter}`;
}
