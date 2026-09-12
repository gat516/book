import type { RecordsRebuildStatus } from "./types";

export function graphCoverageLabel(status: RecordsRebuildStatus): string {
  if (!status.active_generation_id) return "Knowledge graph has not been started for this book.";
  const coverage = `${status.published_chapters}/${status.eligible_chapters} chapters published`;
  return status.has_predecessor
    ? `Replacement graph is active: ${coverage} (${status.missing_chapters} remaining).`
    : `Current graph coverage: ${coverage}.`;
}

export function chapterKnowledgeReviewLabel(chapter: number): string {
  return `Review knowledge added in chapter ${chapter}`;
}
