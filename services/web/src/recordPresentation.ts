import type { SpanView, TermRenderingView } from "./types";

/**
 * Return each terminology decision once, in the order it first appears in the
 * chapter. DISPLAY_SCAN can emit several spans for one source term; repeating
 * that row in the chapter summary makes a long chapter noisy and hides the
 * decisions that still need review.
 */
export function uniqueChapterRenderings(spans: SpanView[]): TermRenderingView[] {
  const seen = new Set<string>();
  const renderings: TermRenderingView[] = [];
  for (const span of spans) {
    const rendering = span.rendering;
    if (!rendering) continue;
    const key = `${rendering.source_term}\u0000${rendering.target_term ?? ""}`;
    if (seen.has(key)) continue;
    seen.add(key);
    renderings.push(rendering);
  }
  return renderings;
}
