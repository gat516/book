import type { SpanView } from "./types";

export interface Segment {
  text: string;
  mention: boolean;
  entityId: string | null;
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
    segments.push({ text: chars.slice(span.char_start, span.char_end).join(""), mention: true, entityId: span.entity_id });
    cursor = span.char_end;
  }
  if (cursor < chars.length) segments.push({ text: chars.slice(cursor).join(""), mention: false, entityId: null });
  return segments;
}

// Reduce a chapter's spans to ONE per distinct thing: its last mention.
//
// Every occurrence used to be a button, which turned ordinary prose into a wall of
// clickable text and made the highlighting worthless as a signal — if everything is
// marked, nothing is. Keeping the LAST mention rather than the first means the anchor sits
// where the chapter has finished saying whatever it had to say about that thing, which is
// also where a fact learned here is most likely to have just been established.
//
// Unlinked mentions have no entity to group by, so they group by their surface text: two
// separate names stay separately reachable, while repeats of one name collapse like any
// other entity. Offsets are Unicode codepoints, matching segment() above.
export function lastMentionPerEntity(text: string, spans: SpanView[]): SpanView[] {
  const chars = Array.from(text);
  const latest = new Map<string, SpanView>();
  for (const span of spans) {
    const surface = chars.slice(span.char_start, span.char_end).join("");
    const key = span.entity_id ? `id:${span.entity_id}` : `text:${surface}`;
    const held = latest.get(key);
    if (!held || span.char_start > held.char_start) latest.set(key, span);
  }
  return [...latest.values()].sort((a, b) => a.char_start - b.char_start);
}
