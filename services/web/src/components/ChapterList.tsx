import { useCallback, useEffect, useId, useRef, useState } from "react";
import { listChapters } from "../api";
import type { ChapterListItem, ChapterListResponse, PipelineStatusResponse } from "../types";
import { PipelineStatus } from "./PipelineStatus";

interface Props {
  novelId: string;
  currentChapter: number;
  onOpen: (chapter: ChapterListItem) => void;
  onClose?: () => void;
  onAdd: () => void;
}

const PAGE_SIZE = 100;

// Fetch only the selected range. Browsing this metadata never advances reading progress
// or starts translation; those actions belong to an explicit chapter selection (§0.3).
export function ChapterList({ novelId, currentChapter, onOpen, onClose, onAdd }: Props) {
  const [page, setPage] = useState<{ offset: number; response: ChapterListResponse } | null>(null);
  const [offset, setOffset] = useState(() => Math.max(0, Math.floor((currentChapter - 1) / PAGE_SIZE) * PAGE_SIZE));
  const [error, setError] = useState<string | null>(null);
  const [processing, setProcessing] = useState<number[]>([]);
  const requestId = useRef(0);
  const tabs = useRef<(HTMLButtonElement | null)[]>([]);
  const id = useId();

  const receiveStatus = useCallback((status: PipelineStatusResponse | null) => {
    const next = status?.in_flight.map((c) => c.chapter_index) ?? [];
    setProcessing((previous) => previous.join(",") === next.join(",") ? previous : next);
  }, []);

  const load = useCallback(async () => {
    const request = ++requestId.current;
    setError(null);
    try {
      const response = await listChapters(novelId, PAGE_SIZE, offset);
      if (request !== requestId.current) return;
      // Recover if the saved position is beyond the chapters currently available.
      const lastOffset = Math.max(0, Math.ceil(response.total / PAGE_SIZE) - 1) * PAGE_SIZE;
      if (offset > lastOffset) setOffset(lastOffset);
      setPage((previous) => previous?.offset === offset &&
        JSON.stringify(previous.response) === JSON.stringify(response)
        ? previous : { offset, response });
    } catch (err) {
      if (request === requestId.current) setError(String(err));
    }
  }, [novelId, offset]);

  useEffect(() => {
    void load();
    // Ignore late responses after switching ranges or leaving the chapter list.
    return () => { requestId.current++; };
  }, [load]);

  const total = page?.response.total ?? 0;
  const pageCount = Math.ceil(total / PAGE_SIZE);
  const activePage = offset / PAGE_SIZE;
  const chapters = page?.offset === offset ? page.response.chapters : null;
  const shownFrom = total === 0 ? 0 : offset + 1;
  const shownTo = Math.min(offset + PAGE_SIZE, total);

  function focusTab(index: number) {
    setOffset(index * PAGE_SIZE);
    tabs.current[index]?.focus();
  }

  return (
    <div className="chapter-list">
      <div className="chapter-list-header">
        <h2>All chapters</h2>
        {onClose && <button onClick={onClose}>← Back to reading</button>}
      </div>

      {/* Reuse the status poll; refresh rows only when pipeline activity changes. */}
      <PipelineStatus novelId={novelId} onProgress={load} onStatus={receiveStatus} />

      <div className="chapter-list-summary">
        <span>{page ? `${total} chapter(s)` : "Loading chapters…"}</span>
        <button onClick={onAdd}>+ Add chapter</button>
      </div>

      {pageCount > 0 && (
        <div className="chapter-range-tabs" role="tablist" aria-label="Chapter ranges">
          {Array.from({ length: pageCount }, (_, index) => (
            <button
              key={index}
              ref={(element) => { tabs.current[index] = element; }}
              type="button"
              role="tab"
              id={`${id}-tab-${index}`}
              aria-selected={index === activePage}
              aria-controls={`${id}-panel`}
              tabIndex={index === activePage ? 0 : -1}
              onClick={() => setOffset(index * PAGE_SIZE)}
              onKeyDown={(event) => {
                let next: number;
                switch (event.key) {
                  case "ArrowRight": next = (index + 1) % pageCount; break;
                  case "ArrowLeft": next = (index + pageCount - 1) % pageCount; break;
                  case "Home": next = 0; break;
                  case "End": next = pageCount - 1; break;
                  default: return;
                }
                event.preventDefault();
                focusTab(next);
              }}
            >
              {index * PAGE_SIZE + 1}–{(index + 1) * PAGE_SIZE}
            </button>
          ))}
        </div>
      )}

      <section
        id={`${id}-panel`}
        role={pageCount > 0 ? "tabpanel" : undefined}
        aria-labelledby={pageCount > 0 ? `${id}-tab-${activePage}` : undefined}
        aria-busy={chapters === null && !error}
        tabIndex={pageCount > 0 ? 0 : undefined}
      >
        {error && <p role="alert" className="chapter-list-error">
          Could not load chapters: {error} <button onClick={() => void load()}>Retry</button>
        </p>}
        {!chapters && !error && <p role="status">Loading chapter range…</p>}
        {chapters && chapters.length === 0 && <p>No chapters yet. Add a chapter to start this book.</p>}
        {chapters && chapters.length > 0 && (
          <>
            <p className="chapter-list-count">Showing {shownFrom}–{shownTo} of {total}</p>
            <table>
              <thead>
                <tr><th>#</th><th>Source chapter</th><th>Status</th><th>Actions</th></tr>
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
                      {chapter.status === "done" && chapter.translation_warning && <small> · Terminology warning</small>}
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
          </>
        )}
      </section>
    </div>
  );
}
