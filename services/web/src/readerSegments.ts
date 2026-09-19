import type { SpanView } from "./types";

export interface Segment {
  text: string;
  mention: boolean;
  entityId: string | null;
  rendering?: SpanView["rendering"];
}

export interface RenderedChapter {
  text: string;
  spans: SpanView[];
}

// Apply CONFIRMED spellings as a presentation overlay instead of mutating the saved
// translation (§0.2), so a correction shows at once while its respell is pending. A
// pending guess is never painted over the text: the translator may have written the
// right name ("Ares") where the guess was wrong ("Aruisi"). Recalculate every downstream
// codepoint offset so a longer or shorter spelling cannot move hover anchors onto
// unrelated prose.
export function applyRenderingChoices(text: string, spans: SpanView[]): RenderedChapter {
  const chars = Array.from(text);
  const output: string[] = [];
  const adjusted: SpanView[] = [];
  let cursor = 0;
  for (const span of [...spans].sort((a, b) => a.char_start - b.char_start)) {
    if (!Number.isInteger(span.char_start) || !Number.isInteger(span.char_end) ||
        span.char_start < cursor || span.char_end <= span.char_start || span.char_end > chars.length) continue;
    output.push(...chars.slice(cursor, span.char_start));
    const start = output.length;
    const replacement = span.rendering?.status === "locked" && span.rendering.target_term
      ? Array.from(span.rendering.target_term)
      : chars.slice(span.char_start, span.char_end);
    output.push(...replacement);
    adjusted.push({ ...span, char_start: start, char_end: output.length });
    cursor = span.char_end;
  }
  output.push(...chars.slice(cursor));
  return { text: output.join(""), spans: adjusted };
}

export function segment(text: string, spans: SpanView[]): Segment[] {
  // The pipeline emits Unicode codepoint offsets, not JavaScript UTF-16 offsets.
  const chars = Array.from(text);
  const segments: Segment[] = [];
  let cursor = 0;
  for (const span of [...spans].sort((a, b) => a.char_start - b.char_start)) {
    if (!Number.isInteger(span.char_start) || !Number.isInteger(span.char_end) ||
        span.char_start < cursor || span.char_end <= span.char_start || span.char_end > chars.length) continue;
    if (span.char_start > cursor) {
      segments.push({ text: chars.slice(cursor, span.char_start).join(""), mention: false, entityId: null });
    }
    segments.push({
      text: chars.slice(span.char_start, span.char_end).join(""),
      mention: true,
      entityId: span.entity_id,
      ...(span.rendering ? { rendering: span.rendering } : {}),
    });
    cursor = span.char_end;
  }
  if (cursor < chars.length) segments.push({ text: chars.slice(cursor).join(""), mention: false, entityId: null });
  return segments;
}
