import type { TermRenderingView, WikiPageSummary } from "./types";

// Navigation uses the wiki's stored source key. Matching translated display names
// would infer identity from spelling and can connect unrelated characters (§0).
export function wikiPageForTerm(pages: WikiPageSummary[], rendering?: TermRenderingView): WikiPageSummary | undefined {
  if (!rendering) return undefined;
  const matches = pages.filter(page => page.source_term === rendering.source_term);
  return matches.length === 1 ? matches[0] : undefined;
}
