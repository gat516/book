import { useEffect, useState } from "react";
import { ApiError, getChapterPreview, getProgress, listChapters, putProgress } from "./api";
import { AddChapterForm } from "./components/AddChapterForm";
import { AskBox } from "./components/AskBox";
import { ChapterList } from "./components/ChapterList";
import { ChapterPending } from "./components/ChapterPending";
import { GlossaryView } from "./components/GlossaryView";
import { NovelCreateForm } from "./components/NovelCreateForm";
import { NovelPicker } from "./components/NovelPicker";
import { ProgressControls } from "./components/ProgressControls";
import { ReaderPane } from "./components/ReaderPane";
import { TranslationNotice } from "./components/TranslationNotice";
import { QueueControls } from "./components/QueueControls";
import type { ChapterListItem, ChapterResponse } from "./types";

// `?novel=` is preferred over a hardcoded default so the app is shareable/demoable via
// URL; falls back to VITE_NOVEL_ID so a configured single-novel deployment isn't blank.
// With neither set, App renders the picker/create-form below (PLAN.md Phase N1) instead
// of the earlier hard error.
function novelIdFromLocation(): string | null {
  const fromQuery = new URLSearchParams(window.location.search).get("novel");
  return fromQuery ?? (import.meta.env.VITE_NOVEL_ID as string | undefined) ?? null;
}

function setNovelInLocation(novelId: string | null) {
  const url = new URL(window.location.href);
  if (novelId) {
    url.searchParams.set("novel", novelId);
  } else {
    url.searchParams.delete("novel");
  }
  window.history.pushState({}, "", url);
}

interface PendingChapter {
  index: number;
  siteChapterNo?: string;
}

const CLICKABLE_ENTITIES_KEY = "reader-clickable-entities";

function savedClickableEntities(): boolean {
  try { return localStorage.getItem(CLICKABLE_ENTITIES_KEY) === "true"; }
  catch { return false; }
}

export default function App() {
  const [novelId, setNovelId] = useState(novelIdFromLocation);
  const [creating, setCreating] = useState(false);
  const [chapterIndex, setChapterIndex] = useState(1);
  // Restore the position to highlight its range, without opening a chapter or advancing
  // the spoiler gate. Reading starts only after an explicit chapter selection (§0.3).
  const [progressLoaded, setProgressLoaded] = useState(false);
  const [chapter, setChapter] = useState<ChapterResponse | null>(null);
  const [addingChapter, setAddingChapter] = useState(false);
  const [nextChapterIndex, setNextChapterIndex] = useState(1);
  const [navigationError, setNavigationError] = useState<string | null>(null);
  // A chapter the reader opened that the pipeline hasn't finished translating. Rendering
  // ChapterPending for it holds them here and polls until it's readable.
  const [pending, setPending] = useState<PendingChapter | null>(null);
  const [showGlossary, setShowGlossary] = useState(false);
  const [showChapters, setShowChapters] = useState(true);
  const [clickableEntities, setClickableEntities] = useState(savedClickableEntities);

  function changeClickableEntities(enabled: boolean) {
    setClickableEntities(enabled);
    try { localStorage.setItem(CLICKABLE_ENTITIES_KEY, String(enabled)); }
    catch { /* The control still works when browser storage is unavailable. */ }
  }

  // A new reader has no progress row (404); show the first chapter range in that case.
  useEffect(() => {
    if (!novelId) return;
    let cancelled = false;
    setProgressLoaded(false);
    getProgress(novelId)
      .then((progress) => {
        if (cancelled) return;
        setChapterIndex(progress.current_chapter > 0 ? progress.current_chapter : 1);
      })
      .catch((err) => {
        if (cancelled) return;
        if (!(err instanceof ApiError && err.status === 404)) {
          console.error("could not restore reading position", err);
        }
        setChapterIndex(1);
      })
      .finally(() => {
        if (!cancelled) setProgressLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, [novelId]);

  function chooseNovel(id: string) {
    setNovelInLocation(id);
    setNovelId(id);
    setCreating(false);
    setProgressLoaded(false);
    setChapterIndex(1);
    setChapter(null);
    setNavigationError(null);
    setAddingChapter(false);
    setPending(null);
    setShowGlossary(false);
    setShowChapters(true);
  }

  function backToNovels() {
    setNovelInLocation(null);
    setNovelId(null);
    setChapter(null);
    setNavigationError(null);
    setAddingChapter(false);
    setPending(null);
    setShowGlossary(false);
    setShowChapters(true);
  }

  function handleNoChapter() {
    setChapter(null);
    setPending(null);
    setShowChapters(true);
  }

  async function startAddingChapter() {
    setNavigationError(null);
    try {
      const list = await listChapters(novelId!, 1, 0);
      setNextChapterIndex(list.total + 1);
      setAddingChapter(true);
    } catch (err) {
      setNavigationError(String(err));
    }
  }

  // Land on `index`, clearing whatever view was covering the reader.
  function goToChapter(index: number) {
    setChapterIndex(index);
    setChapter(null);
    setNavigationError(null);
    setPending(null);
    setShowChapters(false);
    setShowGlossary(false);
  }

  function chapterAdded(index: number) {
    setAddingChapter(false);
    setShowChapters(false);
    // A freshly pasted/scraped chapter is status='ingested' until the worker finishes it,
    // so route straight to the pending view rather than bouncing off an unreadable fetch.
    setPending({ index });
    setChapterIndex(index);
  }

  function chapterLoaded(response: ChapterResponse) {
    setChapter(response);
    setPending(null); // confirmed readable
  }

  async function navigateChapter(index: number) {
    const latest = await getChapterPreview(novelId!, index);
    if (!latest.status) throw new Error("This chapter has not been ingested yet.");
    if (latest.status !== "done") {
      setChapter(null);
      setPending({ index });
      setChapterIndex(index);
      setShowChapters(false);
      setShowGlossary(false);
      return;
    }
    await putProgress(novelId!, index);
    goToChapter(index);
  }

  async function openChapter(item: ChapterListItem) {
    if (item.status !== "done") {
      setChapter(null);
      setPending({ index: item.chapter_index, siteChapterNo: item.site_chapter_no });
      setChapterIndex(item.chapter_index);
      setShowChapters(false);
      return;
    }
    // Reading a chapter means having progressed to it; progress only ever moves forward
    // (reader-api uses GREATEST), so jumping back to an earlier chapter is safe here.
    try {
      await putProgress(novelId!, item.chapter_index);
    } catch (err) {
      console.error("could not advance progress", err);
    }
    goToChapter(item.chapter_index);
  }

  if (!novelId) {
    if (creating) {
      return <NovelCreateForm onCreated={chooseNovel} onCancel={() => setCreating(false)} />;
    }
    return <><QueueControls novelId={null} /><NovelPicker onSelect={chooseNovel} onCreateNew={() => setCreating(true)} /></>;
  }

  return (
    <main className="app">
      <QueueControls novelId={novelId} />
      <button className="app-back" onClick={backToNovels}>
        ← All novels
      </button>

      <button className="app-toggle-glossary" onClick={() => setShowGlossary((v) => !v)}>
        {showGlossary ? "← Close glossary" : "Glossary"}
      </button>
      <details className="reader-settings">
        <summary>Reading settings</summary>
        <label><input type="checkbox" checked={!clickableEntities} onChange={(event) => changeClickableEntities(!event.target.checked)} /> Show hover previews</label>
        <p>Highlighted names are always clickable, even when no information is linked yet. Enable previews to also see a card on hover. Saved in this browser.</p>
      </details>
      {showGlossary && <GlossaryView key={novelId} novelId={novelId} at={chapter?.at} />}
      <div hidden={showGlossary}>
        {navigationError && <p role="alert" className="chapter-list-error">{navigationError}</p>}
        {addingChapter ? (
          <AddChapterForm
            novelId={novelId}
            nextChapterIndex={nextChapterIndex}
            onAdded={chapterAdded}
            onCancel={() => setAddingChapter(false)}
          />
        ) : !progressLoaded ? (
          <p>Loading chapters…</p>
        ) : showChapters ? (
          <ChapterList
            key={novelId}
            novelId={novelId}
            currentChapter={chapterIndex}
            onOpen={openChapter}
            onClose={chapter || pending ? () => setShowChapters(false) : undefined}
            onAdd={startAddingChapter}
          />
        ) : pending ? (
          <ChapterPending
            key={`${novelId}:${pending.index}`}
            novelId={novelId}
            chapterIndex={pending.index}
            siteChapterNo={pending.siteChapterNo}
            onReady={() => goToChapter(pending.index)}
            onBack={() => setShowChapters(true)}
          />
        ) : (
          <>
            {/* The outer hidden container keeps the reader mounted while editing terms. */}
            <div>
              {/* Above the text rather than over it: a caveat about the translation should
                  be visible before reading, without interrupting it. */}
              <TranslationNotice novelId={novelId} />
              <ReaderPane
                novelId={novelId}
                chapterIndex={chapterIndex}
                clickableEntities={clickableEntities}
                onChapterLoaded={chapterLoaded}
                onNoChapter={handleNoChapter}
              />
            </div>
            <ProgressControls
              chapterIndex={chapterIndex}
              hasNext={chapter?.has_next ?? false}
              onNavigate={navigateChapter}
            />
            <button className="app-chapters" onClick={() => setShowChapters(true)}>
              All chapters
            </button>
            <button className="app-add-chapter" onClick={startAddingChapter}>
              + Add chapter
            </button>
            {chapter && !showGlossary && <AskBox novelId={novelId} at={chapter.at} />}
          </>
        )}
      </div>
    </main>
  );
}
