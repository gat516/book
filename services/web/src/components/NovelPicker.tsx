import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { deleteNovel, listNovels, listProviderCredentials } from "../api";
import { ArrowRight, BookOpen, MoreHorizontal, Plus, Search, Settings2, ShieldCheck } from "lucide-react";
import { Button, Fade } from "./animate-ui/motion";
import { hostedSession } from "../session";
import type { NovelSummary } from "../types";
import { FirstBookGuide } from "./FirstBookGuide";

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
  const [query, setQuery] = useState("");
  const [hasKey, setHasKey] = useState<boolean | null>(null);
  const [showGuide, setShowGuide] = useState(false);
  // Two-step delete: the first click arms this novel, the second commits. A confirm()
  // dialog would do the same job, but deleting a novel throws away every chapter and the
  // whole graph built from it, so the confirmation names what is about to go.
  const [armed, setArmed] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);

  useEffect(() => {
    listNovels()
      .then((response) => setNovels(response.novels))
      .catch((err) => setError(String(err)));
    listProviderCredentials().then(response => setHasKey(response.credentials.some(c => c.api_key_set))).catch(() => setHasKey(null));
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

  const filtered = novels?.filter(n => n.title.toLocaleLowerCase().includes(query.toLocaleLowerCase()));
  const inProgress = novels?.filter(n => n.current_chapter).length ?? 0;
  return (
    <div className="novel-picker">
      <header className="novel-picker-header">
        <div>
          <p className="eyebrow">A LITTLE CORNER OF YOUR OWN</p>
          <h1>Your library<span className="heading-period">.</span></h1>
          <p>{novels?.length === 0 ? "Every good story starts with a first chapter." : "Pick up where your curiosity left off."}</p>
        </div>
        <div className="novel-picker-header-actions">
          {onSettings && (
            <button type="button" onClick={onSettings}>
              <Settings2 size={16} /> Account settings
            </button>
          )}
          <Button type="button" className="btn-primary" onClick={onCreateNew}><Plus size={17} />Add book</Button>
        </div>
      </header>
      {novels && novels.length > 0 && <><div className="library-overview"><span><BookOpen size={17} />{novels.length} {novels.length === 1 ? "book" : "books"} on your shelf</span><span>{inProgress} in progress</span><span className="library-private"><ShieldCheck size={15} />Only yours</span></div>{children}</>}
      {error && <p role="alert" className="novel-picker-error">{error}</p>}
      {novels === null && !error && <p className="novel-picker-loading" role="status">Opening your bookshelf…</p>}
      {novels && (novels.length === 0 || showGuide || (hostedSession() && hasKey === false)) && <FirstBookGuide onCreate={onCreateNew} onSettings={onSettings} hasKey={hasKey === true} hasBook={novels.length > 0} onOpen={() => novels[0] && onSelect(novels[0].id)} />}
      {novels && novels.length > 0 && (
        <><div className="shelf-toolbar"><h2>The bookshelf <span>{novels.length.toString().padStart(2, "0")}</span></h2><label className="library-search"><Search size={16} /><span className="visually-hidden">Search your books</span><input value={query} onChange={e => setQuery(e.target.value)} placeholder="Find a story…" type="search" /></label></div>
        {filtered?.length === 0 && <p role="status" className="empty-search">No books match “{query}”. <button onClick={() => setQuery("")}>Clear search</button></p>}
        <ul className="novel-picker-books" aria-label="Books">
          {filtered?.map((novel) => (
            <li key={novel.id} className="novel-picker-book-row" data-cover={novels.indexOf(novel) % 4}>
              <Fade className="book-card-main">
              <button
                type="button"
                className="novel-picker-book"
                onClick={() => onSelect(novel.id)}
                aria-label={`Open ${novel.title}`}
              >
                <span className="library-book-cover" aria-hidden="true"><span className="book-cover-label">YOUR PRIVATE EDITION</span><span className="book-cover-symbol">{novel.title.slice(0, 1).toUpperCase()}</span><span className="book-cover-title">{novel.title}</span><span className="book-cover-foot">{novel.source_lang.toUpperCase()} / {novel.target_lang.toUpperCase()}<BookOpen size={18} /></span></span>
                <span className="book-card-copy">
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
                <span className="book-open-label">{novel.current_chapter ? "Return to your story" : "Open your book"}<ArrowRight size={16} /></span>
                </span>
              </button>
              </Fade>
              <details className="novel-picker-book-menu">
                <summary aria-label={`More actions for ${novel.title}`}><MoreHorizontal size={19} /></summary>
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
        </ul><div className="library-bottom-note"><span>Your next chapter is right where you left it.</span><button className="text-button" aria-expanded={showGuide} onClick={() => setShowGuide(!showGuide)}>{showGuide ? "Hide getting started" : "Getting started"}</button></div></>
      )}
    </div>
  );
}
