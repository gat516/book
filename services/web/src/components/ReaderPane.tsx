import { useEffect, useMemo, useState } from "react";
import { ApiError, getChapter, putProgress } from "../api";
import type { ChapterResponse, EntityView } from "../types";
import { HoverCard } from "./HoverCard";
import { EntityInspector } from "./EntityInspector";

interface Props {
  novelId: string;
  chapterIndex: number;
  clickableEntities: boolean;
  onChapterLoaded: (chapter: ChapterResponse) => void;
  // Called instead of rendering an error when the requested chapter (and typically every
  // chapter — a brand-new novel) doesn't exist yet, so the caller can offer to add one
  // instead of showing a raw "chapter is missing or not done" string.
  onNoChapter: () => void;
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

export function ReaderPane({ novelId, chapterIndex, clickableEntities, onChapterLoaded, onNoChapter }: Props) {
  const [chapter, setChapter] = useState<ChapterResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [hovered, setHovered] = useState<string | null>(null);
  const [selected, setSelected] = useState<{ id: string; mention: string } | null>(null);

  // Both hover and click views share only the exact novel/chapter/clearance cache.
  // The server's `at` becomes known on load; changing it discards earlier entity data.
  const cache = useMemo(() => new Map<string, EntityView>(), [novelId, chapterIndex, chapter?.at]);

  useEffect(() => {
    setSelected(null);
    setHovered(null);
  }, [novelId, chapterIndex, clickableEntities]);

  useEffect(() => {
    let cancelled = false;
    setChapter(null);
    setError(null);

    async function load(): Promise<ChapterResponse | "no-chapter"> {
      try {
        return await getChapter(novelId, chapterIndex);
      } catch (err) {
        if (err instanceof ApiError && err.status === 404 && err.code === "chapter not found") {
          return "no-chapter";
        }
        // A brand-new reader has no `reader_progress` row yet, so every gated endpoint
        // 404s — including this one — until one exists (reader-api's design, not a bug).
        // A first-time reader just wants to start reading, so bootstrap progress to this
        // chapter and retry once, rather than surfacing a raw 404 as the landing state.
        if (err instanceof ApiError && err.status === 404 && err.code === "reader progress not found") {
          try {
            await putProgress(novelId, chapterIndex);
          } catch (putErr) {
            // The novel has no chapters yet (or not this one) — AdvanceProgress only
            // succeeds against a chapter with status='done', so this 409 means "nothing
            // to read here" rather than a real failure.
            if (putErr instanceof ApiError && putErr.status === 409) {
              return "no-chapter";
            }
            throw putErr;
          }
          return await getChapter(novelId, chapterIndex);
        }
        throw err;
      }
    }

    load()
      .then((response) => {
        if (cancelled) return;
        if (response === "no-chapter") {
          onNoChapter();
          return;
        }
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
      <p className="reader-pane-chapter-label">
        Chapter {chapterIndex}
        {chapter.site_chapter_no && (
          <span className="reader-pane-site-chapter-no">
            {" "}
            — {chapter.site_chapter_no}
            {/* This site paginates a chapter across several pages, each ingested as its
                own chapter row; without the part number a reader can't tell why the text
                stops mid-scene. Hidden when the chapter isn't split. */}
            {chapter.part > 1 && ` (part ${chapter.part})`}
          </span>
        )}
      </p>
      {clickableEntities && chapter.spans.length === 0 && <p className="reader-entity-hint">
        No linked entities in this chapter yet. Names become clickable when the pipeline records their mentions.
      </p>}
      {segments.map((piece, index) =>
        piece.entityId ? (
          clickableEntities ? <button
            key={index}
            type="button"
            className="mention mention-button"
            aria-haspopup="dialog"
            aria-label={`Inspect ${piece.text}`}
            onClick={() => setSelected({ id: piece.entityId!, mention: piece.text })}
          >{piece.text}</button> :
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
      {clickableEntities && selected && <EntityInspector
        key={`${novelId}:${chapterIndex}:${chapter.at}:${selected.id}`}
        novelId={novelId}
        entityId={selected.id}
        mention={selected.mention}
        at={chapter.at}
        cache={cache}
        onClose={() => setSelected(null)}
      />}
    </div>
  );
}
