import { useCallback, useEffect, useState } from "react";
import { listChapters } from "../api";
import type { ChapterListItem, PipelineStatusResponse } from "../types";
import { PipelineStatus } from "./PipelineStatus";

interface Props {
  novelId: string;
  currentChapter: number;
  onOpen: (chapter: ChapterListItem) => void;
  onClose: () => void;
}

// 25, not 100: every row is re-rendered whenever the list refreshes, and a hundred rows
// refreshing alongside the other live views on this page was enough to make the whole UI
// lag. Paging is cheap; re-rendering a long table repeatedly is not.
const PAGE_SIZE = 25;

// The chapter index. A scraped novel can run to thousands of chapters, so this pages
// server-side (never fetching the whole list) and offers a jump-to box — paging one screen
// at a time is unusable at that scale.
//
// Durable chapter state and live worker claims are separate: "ingested" means
// not queued, and only a live claim is evidence of processing.
export function ChapterList({ novelId, currentChapter, onOpen, onClose }: Props) {
  const [chapters, setChapters] = useState<ChapterListItem[] | null>(null);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(() => Math.max(0, Math.floor((currentChapter - 1) / PAGE_SIZE) * PAGE_SIZE));
  const [error, setError] = useState<string | null>(null);
  const [jumpTo, setJumpTo] = useState("");
  // Collapsed by default. The table is a row per chapter and the heaviest thing on this
  // page, so a novel with hundreds of chapters shouldn't render one just because you
  // opened the view.
  const [expanded, setExpanded] = useState(false);
  const [processing, setProcessing] = useState<number[]>([]);
  const receiveStatus = useCallback((status: PipelineStatusResponse | null) => {
    const next = status?.in_flight.map((c) => c.chapter_index) ?? [];
    setProcessing((previous) => previous.join(",") === next.join(",") ? previous : next);
  }, []);

  const load = useCallback(() => {
    // While collapsed only the total is displayed, so ask for a single row rather than a
    // full page nothing will render.
    listChapters(novelId, expanded ? PAGE_SIZE : 1, expanded ? offset : 0)
      .then((response) => {
        setChapters(expanded ? response.chapters : null);
        setTotal(response.total);
        setError(null);
      })
      .catch((err) => setError(String(err)));
  }, [novelId, offset, expanded]);

  useEffect(load, [load]);

  function jump(e: React.FormEvent) {
    e.preventDefault();
    const target = Number(jumpTo);
    if (!Number.isFinite(target) || target < 1) return;
    setOffset(Math.max(0, Math.floor((target - 1) / PAGE_SIZE) * PAGE_SIZE));
    setJumpTo("");
  }

  const shownFrom = total === 0 ? 0 : offset + 1;
  const shownTo = Math.min(offset + PAGE_SIZE, total);

  return (
    <div className="chapter-list">
      <div className="chapter-list-header">
        <h2>Chapters</h2>
        <button onClick={onClose}>← Back to reading</button>
      </div>

      {error && <p className="chapter-list-error">Could not load chapters: {error}</p>}

      {/* One timer for the page: PipelineStatus is already polling, so the list reloads
          when it reports the worker actually moved on — instead of running a second
          interval that re-rendered the whole table on a fixed beat. */}
      <PipelineStatus novelId={novelId} onProgress={load} onStatus={receiveStatus} />

      <div className="chapter-list-summary">
        <span>{total} chapter(s)</span>
        <button onClick={() => setExpanded((v) => !v)}>
          {expanded ? "Hide chapters" : "Show chapters"}
        </button>
      </div>

      {!expanded && (
        <p className="chapter-list-collapsed-hint">
          The chapter table is hidden. Showing it renders a row per chapter, which is the
          heaviest thing on this page.
        </p>
      )}

      {expanded && (
        <>
      <p className="chapter-list-count">
        Showing {shownFrom}–{shownTo} of {total}
      </p>

      <form className="chapter-list-jump" onSubmit={jump}>
        <label>
          Jump to chapter
          <input
            type="number"
            min={1}
            value={jumpTo}
            onChange={(e) => setJumpTo(e.target.value)}
            placeholder="e.g. 250"
          />
        </label>
        <button type="submit" disabled={!jumpTo}>
          Go
        </button>
      </form>

      <div className="chapter-list-pager">
        <button disabled={offset <= 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>
          ← Previous {PAGE_SIZE}
        </button>
        <button disabled={offset + PAGE_SIZE >= total} onClick={() => setOffset(offset + PAGE_SIZE)}>
          Next {PAGE_SIZE} →
        </button>
      </div>

      {!chapters && !error && <p>Loading chapters…</p>}
      {chapters && chapters.length === 0 && <p>No chapters ingested yet.</p>}

      {chapters && chapters.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>#</th>
              <th>Source chapter</th>
              <th>Status</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {chapters.map((chapter) => (
              <tr
                key={chapter.chapter_index}
                className={chapter.chapter_index === currentChapter ? "chapter-list-current" : undefined}
              >
                <td>{chapter.chapter_index}</td>
                <td>
                  {chapter.site_chapter_no ?? "—"}
                  {/* Only worth showing once a chapter is actually split; "Part 1" on
                      every ordinary chapter is noise, not information. */}
                  {chapter.part > 1 && <span className="chapter-list-part"> · Part {chapter.part}</span>}
                </td>
                <td>
                  {chapter.status === "done"
                    ? "Ready"
                    : processing.includes(chapter.chapter_index)
                      ? "Processing"
                      : chapter.status === "ingested"
                        ? "Not queued"
                        : chapter.status === "queued"
                          ? "Queued"
                          : chapter.status === "error"
                            ? "Failed"
                            : chapter.status}
                  {chapter.status === "done" && chapter.graph_status && chapter.graph_status !== "done" && (
                    <small> · {chapter.graph_status === "error" ? "Facts unavailable" : "Facts pending"}</small>
                  )}
                </td>
                <td>
                  <button onClick={() => onOpen(chapter)}>
                    {chapter.status === "done" ? "Read" : "View status"}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
        </>
      )}
    </div>
  );
}
