import { useCallback, useEffect, useId, useRef, useState } from "react";
import { listChapters } from "../api";
import type { ChapterListItem, ChapterListResponse, PipelineStatusResponse } from "../types";
import { PipelineStatus } from "./PipelineStatus";
import { providerFailureDetail } from "./ProviderHealth";
import { FactsControls } from "./FactsControls";
import { readerFeaturesState, readingState } from "../chapterStatus";

interface Props {
  novelId: string;
  currentChapter: number;
  onOpen: (chapter: ChapterListItem) => void;
  onClose?: () => void;
  onAdd: () => void;
  onSettings?: () => void;
}

const PAGE_SIZE = 100;

// Fetch only the selected range. Browsing this metadata never advances reading progress
// or starts translation; those actions belong to an explicit chapter selection (§0.3).
export function ChapterList({ novelId, currentChapter, onOpen, onClose, onAdd, onSettings }: Props) {
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
        <h2>Chapters</h2>
        {onClose && <button onClick={onClose}>← Back to reading</button>}
      </div>

      {/* Reuse the status poll; refresh rows only when pipeline activity changes. */}
      <PipelineStatus novelId={novelId} onProgress={load} onStatus={receiveStatus} />
      {total > 0 && <FactsControls novelId={novelId} />}

      <div className="chapter-list-summary">
        <span>{page ? `${total} chapter(s)` : "Loading chapters…"}</span>
        <button className="btn-primary" onClick={onAdd}>Add chapter</button>
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
        {chapters && chapters.length === 0 && <section className="chapter-welcome"><p className="eyebrow">Your book is ready</p><h3>Let’s add its first chapter.</h3><p>Save your AI provider key before processing text, then paste a chapter or import from a supported website. Your translation and story wiki will build as you go.</p><div className="hero-actions">{onSettings && <button onClick={onSettings}>Set up provider key</button>}<button className="btn-primary" onClick={onAdd}>Add first chapter →</button></div></section>}
        {chapters && chapters.length > 0 && (
          <>
            <p className="chapter-list-count">Showing {shownFrom}–{shownTo} of {total}</p>
            <ul className="chapter-rows">
              {chapters.map((chapter) => {
                const active = processing.includes(chapter.chapter_index);
                const reading = readingState(chapter, active);
                const features = readerFeaturesState(chapter, active);
                const isCurrent = chapter.chapter_index === currentChapter;
                // One chip: the text's own state. What is still being built for the wiki
                // is a quiet second line, and only while it is not finished -- two
                // columns of "Ready" needed a paragraph above the table to explain them.
                const note = chapter.status === "error" && chapter.failure_category
                  ? providerFailureDetail(chapter.failure_category)
                  : chapter.status === "done" && chapter.translation_warning
                    ? "Some saved names need review"
                    : reading.label === "Ready to read" && features.label !== "Ready"
                      ? `Names and facts: ${features.label.toLowerCase()}`
                      : null;
                return <li key={chapter.chapter_index} className={`chapter-row${isCurrent ? " is-current" : ""}`}>
                  <button type="button" className="chapter-row-open" onClick={() => onOpen(chapter)}
                    aria-label={`${chapter.status === "done" ? "Read" : "View status for"} chapter ${chapter.chapter_index}: ${reading.label}`}>
                    <span className="chapter-row-index">{chapter.chapter_index}</span>
                    <span className="chapter-row-title" title={chapter.site_chapter_no || undefined}>
                      {chapter.site_chapter_no ?? `Chapter ${chapter.chapter_index}`}
                      {chapter.part > 1 && <span className="chapter-row-part"> · part {chapter.part}</span>}
                    </span>
                    {isCurrent && <span className="chapter-row-current">Current</span>}
                    <span className={`status-pill status-pill-${reading.tone}`}>
                      {reading.tone === "live" && <span className="reader-records-dot" aria-hidden="true" />}
                      {reading.label}
                    </span>
                  </button>
                  {chapter.source_url && <a className="chapter-row-source" href={chapter.source_url}
                    target="_blank" rel="noreferrer" aria-label={`Open chapter ${chapter.chapter_index} on the source site`}>Source ↗</a>}
                  {note && <p className="chapter-row-note">{note}</p>}
                </li>;
              })}
            </ul>
          </>
        )}
      </section>
    </div>
  );
}
