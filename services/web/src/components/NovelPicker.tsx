import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { deleteNovel, listNovels } from "../api";
import type { NovelSummary } from "../types";

interface Props {
  onSelect: (novelId: string) => void;
  onBookSettings: (novelId: string) => void;
  onCreateNew: () => void;
  onSettings?: () => void;
  children?: ReactNode;
}

export function NovelPicker({ onSelect, onBookSettings, onCreateNew, onSettings, children }: Props) {
  const [novels, setNovels] = useState<NovelSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Two-step delete: the first click arms this novel, the second commits. A confirm()
  // dialog would do the same job, but deleting a novel throws away every chapter and the
  // whole graph built from it, so the confirmation names what is about to go.
  const [armed, setArmed] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);

  useEffect(() => {
    listNovels()
      .then((response) => setNovels(response.novels))
      .catch((err) => setError(String(err)));
  }, []);

  async function confirmDelete(novel: NovelSummary) {
    setDeleting(novel.id);
    setError(null);
    try {
      await deleteNovel(novel.id);
      setNovels((current) => (current ?? []).filter((n) => n.id !== novel.id));
      setArmed(null);
    } catch (err) {
      setError(`Could not delete ${novel.title}: ${err}`);
    } finally {
      setDeleting(null);
    }
  }

  return (
    <div className="novel-picker">
      <header className="novel-picker-header">
        <div>
          <h1>Library</h1>
          <p>Choose a book to continue reading.</p>
        </div>
        <div className="novel-picker-header-actions">
          {onSettings && (
            <button type="button" onClick={onSettings}>
              Settings
            </button>
          )}
          <button type="button" className="btn-primary" onClick={onCreateNew}>
            Add book
          </button>
        </div>
      </header>
      {children}
      {error && <p className="novel-picker-error">{error}</p>}
      {novels === null && !error && <p className="novel-picker-loading">Loading books…</p>}
      {novels && novels.length === 0 && <p>No books yet.</p>}
      {novels && novels.length > 0 && (
        <ul className="novel-picker-books" aria-label="Books">
          {novels.map((novel) => (
            <li key={novel.id} className="novel-picker-book-row">
              <button
                type="button"
                className="novel-picker-book"
                onClick={() => onSelect(novel.id)}
                aria-label={`Open ${novel.title}`}
              >
                <span className="novel-picker-book-title" title={novel.title}>{novel.title}</span>
                <span className="novel-picker-book-meta">
                  <span className="novel-picker-langs">
                    {novel.source_lang} → {novel.target_lang}
                  </span>
                  {novel.genre && <span>{novel.genre}</span>}
                </span>
                <span className={`novel-picker-progress${novel.current_chapter ? " novel-picker-progress-current" : ""}`}>
                  {novel.current_chapter ? `Chapter ${novel.current_chapter}` : "Not started"}
                </span>
              </button>
              <details className="novel-picker-book-menu">
                <summary aria-label={`More actions for ${novel.title}`}>More</summary>
                <div className="novel-picker-book-menu-panel">
                  {armed === novel.id ? (
                    <div className="novel-picker-confirm" role="group" aria-label={`Confirm deleting ${novel.title}`}>
                      <p>
                        Delete {novel.title} and all its chapters, translations, and reader features? This cannot be undone.
                      </p>
                      <button
                        type="button"
                        className="novel-picker-delete btn-danger"
                        disabled={deleting === novel.id}
                        onClick={() => void confirmDelete(novel)}
                      >
                        {deleting === novel.id ? "Deleting…" : "Delete permanently"}
                      </button>
                      <button type="button" disabled={deleting === novel.id} onClick={() => setArmed(null)}>
                        Cancel
                      </button>
                    </div>
                  ) : (
                    <div className="novel-picker-book-menu-actions">
                      <button type="button" onClick={() => onSelect(novel.id)}>Open book</button>
                      <button type="button" onClick={() => onBookSettings(novel.id)}>Book settings</button>
                      <button type="button" className="novel-picker-delete btn-danger" onClick={() => setArmed(novel.id)}>
                        Delete book
                      </button>
                    </div>
                  )}
                </div>
              </details>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
