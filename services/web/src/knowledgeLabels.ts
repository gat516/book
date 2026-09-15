import type { RecordsRebuildStatus } from "./types";

export function graphCoverageLabel(status: RecordsRebuildStatus): string {
  if (!status.active_generation_id) return "Not started";
  const coverage = `${status.published_chapters} of ${status.eligible_chapters} chapters ready`;
  if (status.has_predecessor && status.missing_chapters > 0) {
    return `${status.running ? "Refreshing" : "Refresh paused"} · ${coverage}`;
  }
  if (status.missing_chapters === 0) return `Up to date · ${coverage}`;
  return `${status.running ? "Building" : "Paused"} · ${coverage}`;
}

export function chapterKnowledgeReviewLabel(chapter: number): string {
  return `Advanced: inspect story details from chapter ${chapter}`;
}
