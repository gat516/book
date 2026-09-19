import type { BookFactsStatus } from "./types";

// Book-wide progress of names and facts: a chapter counts once FACTS has run (0110).
export function factsCoverageLabel(status: BookFactsStatus): string {
  const coverage = `${status.done_chapters} of ${status.eligible_chapters} chapters ready`;
  if (status.eligible_chapters === 0) return "No readable chapters yet";
  if (status.missing_chapters === 0) return `Up to date · ${coverage}`;
  return status.running ? `Working · ${coverage}` : coverage;
}
