import { useEffect, useMemo, useState } from "react";
import { ApiError, getChapter, putProgress } from "../api";
import type { ChapterResponse, EntityView } from "../types";
import { HoverCard } from "./HoverCard";

interface Props {
  novelId: string;
  chapterIndex: number;
  onChapterLoaded: (chapter: ChapterResponse) => void;
}

interface Segment {
  text: string;
  entityId: string | null;
}

// Splits `text` at each span boundary. Offsets are Unicode-codepoint indices (from
// Python's `str` indexing); JS string indexing is UTF-16 code units. The two coincide
// for BMP text and diverge for astral-plane characters (emoji, some CJK extension-B
// ideographs) — an accepted known limitation for Milestone 1, flagged here rather than
// silently assumed correct on real-world text that hits it.
function segment(text: string, spans: { char_start: number; char_end: number; entity_id: string }[]): Segment[] {
  const sorted = [...spans].sort((a, b) => a.char_start - b.char_start);
  const segments: Segment[] = [];
  let cursor = 0;
  for (const span of sorted) {
    if (span.char_start < cursor) continue; // overlapping spans: keep the earlier one
    if (span.char_start > cursor) {
      segments.push({ text: text.slice(cursor, span.char_start), entityId: null });
    }
    segments.push({ text: text.slice(span.char_start, span.char_end), entityId: span.entity_id });
    cursor = span.char_end;
  }
  if (cursor < text.length) {
    segments.push({ text: text.slice(cursor), entityId: null });
  }
  return segments;
}

export function ReaderPane({ novelId, chapterIndex, onChapterLoaded }: Props) {
  const [chapter, setChapter] = useState<ChapterResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [hovered, setHovered] = useState<string | null>(null);

  // A fresh Map whenever (novelId, at) changes IS the hover-card cache key discipline —
  // see HoverCard.tsx. `at` isn't known until the chapter loads, so key on chapterIndex
  // as a stand-in for "this load"; a re-fetch of the same chapter gets a fresh cache too,
  // which is the safe direction (an extra fetch, never a stale cross-progress leak).
  const cache = useMemo(() => new Map<string, EntityView>(), [novelId, chapterIndex]);

  useEffect(() => {
    let cancelled = false;
    setChapter(null);
    setError(null);

    async function load() {
      try {
        return await getChapter(novelId, chapterIndex);
      } catch (err) {
        // A brand-new reader has no `reader_progress` row yet, so every gated endpoint
        // 404s — including this one — until one exists (reader-api's design, not a bug).
        // A first-time reader just wants to start reading, so bootstrap progress to this
        // chapter and retry once, rather than surfacing a raw 404 as the landing state.
        if (err instanceof ApiError && err.status === 404 && err.code === "reader progress not found") {
          await putProgress(novelId, chapterIndex);
          return await getChapter(novelId, chapterIndex);
        }
        throw err;
      }
    }

    load()
      .then((response) => {
        if (cancelled) return;
        setChapter(response);
        onChapterLoaded(response);
      })
      .catch((err) => {
        if (!cancelled) setError(String(err));
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [novelId, chapterIndex]);

  if (error) return <p className="reader-pane-error">Could not load chapter: {error}</p>;
  if (!chapter) return <p>Loading chapter…</p>;

  const segments = segment(chapter.text, chapter.spans);

  return (
    <div className="reader-pane">
      {segments.map((piece, index) =>
        piece.entityId ? (
          <mark
            key={index}
            className="mention"
            onMouseEnter={() => setHovered(piece.entityId)}
            onMouseLeave={() => setHovered((current) => (current === piece.entityId ? null : current))}
          >
            {piece.text}
            {hovered === piece.entityId && (
              <HoverCard
                novelId={novelId}
                entityId={piece.entityId}
                at={chapter.at}
                cache={cache}
                onClose={() => setHovered(null)}
              />
            )}
          </mark>
        ) : (
          <span key={index}>{piece.text}</span>
        ),
      )}
    </div>
  );
}
