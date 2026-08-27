import { useCallback, useEffect, useState } from "react";
import { listChapters } from "../api";
import type { ChapterListItem } from "../types";
import { PipelineStatus } from "./PipelineStatus";

interface Props {
  novelId: string;
  currentChapter: number;
  onOpen: (chapter: ChapterListItem) => void;
  onClose: () => void;
}

const PAGE_SIZE = 100;

// The chapter index. A scraped novel can run to thousands of chapters, so this pages
// server-side (never fetching the whole list) and offers a jump-to box — paging one screen
// at a time is unusable at that scale.
//
// `status` comes straight from the pipeline: "done" is readable, "ingested" means the
// worker hasn't finished translating it yet, "error" means it failed. Showing it here is
// the whole point — without it there's no way to tell an untranslated chapter from a
// missing one.
export function ChapterList({ novelId, currentChapter, onOpen, onClose }: Props) {
  const [chapters, setChapters] = useState<ChapterListItem[] | null>(null);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(() => Math.max(0, Math.floor((currentChapter - 1) / PAGE_SIZE) * PAGE_SIZE));
  const [error, setError] = useState<string | null>(null);
  const [jumpTo, setJumpTo] = useState("");

  const load = useCallback(() => {
    listChapters(novelId, PAGE_SIZE, offset)
      .then((response) => {
        setChapters(response.chapters);
        setTotal(response.total);
        setError(null);
      })
      .catch((err) => setError(String(err)));
  }, [novelId, offset]);

  useEffect(load, [load]);

  // Any chapter still mid-pipeline means this list is a live view, not a snapshot — poll
  // so it flips to "done" on its own instead of needing a manual refresh.
  useEffect(() => {
    if (!chapters?.some((c) => c.status === "ingested")) return;
    const timer = setInterval(load, 5000);
    return () => clearInterval(timer);
  }, [chapters, load]);

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

      <PipelineStatus novelId={novelId} />

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
                    : chapter.status === "ingested"
                      ? "Translating…"
                      : chapter.status}
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
    </div>
  );
}
