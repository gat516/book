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
  // Until stored progress has been resolved we don't know which chapter to open, and
  // mounting the reader on chapter 1 first would fetch a chapter the reader isn't on and
  // then visibly jump. Gate the reader on this instead.
  const [progressLoaded, setProgressLoaded] = useState(false);
  const [chapter, setChapter] = useState<ChapterResponse | null>(null);
  // Distinct from "loading" — set when the requested chapter isn't readable. Note this
  // covers TWO very different situations that the reader endpoints report identically
  // (a 404 for the chapter, then a 409 from bootstrapping progress):
  //
  //   - the novel genuinely has no chapters yet, and
  //   - the novel has chapters, but none have finished translating.
  //
  // Conflating them was a real dead end: a novel with 177 ingested chapters showed the
  // "add a chapter" form and offered no way to reach any of them. hasChapters below is
  // what separates the two.
  const [noChapter, setNoChapter] = useState(false);
  // null = not yet determined. Set when noChapter fires, by asking how many chapters the
  // novel actually holds.
  const [hasChapters, setHasChapters] = useState<boolean | null>(null);
  const [addingChapter, setAddingChapter] = useState(false);
  // A chapter the reader opened that the pipeline hasn't finished translating. Rendering
  // ChapterPending for it holds them here and polls until it's readable.
  const [pending, setPending] = useState<PendingChapter | null>(null);
  const [showGlossary, setShowGlossary] = useState(false);
  const [showChapters, setShowChapters] = useState(false);
  const [clickableEntities, setClickableEntities] = useState(savedClickableEntities);

  function changeClickableEntities(enabled: boolean) {
    setClickableEntities(enabled);
    try { localStorage.setItem(CLICKABLE_ENTITIES_KEY, String(enabled)); }
    catch { /* The control still works when browser storage is unavailable. */ }
  }

  // Reopen the novel where this reader left off. A reader who has never opened it has no
  // progress row (404) — that's the normal first-visit path, not an error, so fall back
  // to chapter 1 and let ReaderPane bootstrap progress as it already does.
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
    setChapterIndex(1);
    setChapter(null);
    setNoChapter(false);
    setHasChapters(null);
    setAddingChapter(false);
    setPending(null);
    setShowGlossary(false);
    setShowChapters(false);
  }

  function backToNovels() {
    setNovelInLocation(null);
    setNovelId(null);
    setChapter(null);
    setNoChapter(false);
    setHasChapters(null);
    setAddingChapter(false);
    setPending(null);
    setShowGlossary(false);
    setShowChapters(false);
  }

  // The reader endpoints can't tell "novel is empty" from "nothing translated yet" — both
  // surface as an unreadable chapter — so ask the chapter index directly and route
  // accordingly: an empty novel wants the add form, a full one wants its chapter list.
  async function handleNoChapter() {
    setNoChapter(true);
    try {
      const list = await listChapters(novelId!, 1, 0);
      setHasChapters(list.total > 0);
      if (list.total > 0) setShowChapters(true);
    } catch {
      setHasChapters(false); // can't tell — fall back to the add form rather than a dead end
    }
  }

  // Land on `index`, clearing whatever view was covering the reader.
  function goToChapter(index: number) {
    setChapterIndex(index);
    setChapter(null);
    setNoChapter(false);
    setHasChapters(null);
    setPending(null);
    setShowChapters(false);
    setShowGlossary(false);
  }

  function chapterAdded(index: number) {
    setAddingChapter(false);
    setNoChapter(false);
    setHasChapters(null);
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
    return <NovelPicker onSelect={chooseNovel} onCreateNew={() => setCreating(true)} />;
  }

  return (
    <main className="app">
      <button className="app-back" onClick={backToNovels}>
        ← All novels
      </button>

      <button className="app-toggle-glossary" onClick={() => setShowGlossary((v) => !v)}>
        {showGlossary ? "← Close glossary" : "Glossary"}
      </button>
      <details className="reader-settings">
        <summary>Reading settings</summary>
        <label><input type="checkbox" checked={clickableEntities} onChange={(event) => changeClickableEntities(event.target.checked)} /> Clickable entities</label>
        <p>Click highlighted names to inspect their information and edit glossary terms. Saved in this browser. When off, hover cards remain available.</p>
      </details>
      {showGlossary && <GlossaryView key={novelId} novelId={novelId} at={chapter?.at} />}
      <div hidden={showGlossary}>
        {addingChapter ? (
          <AddChapterForm
            novelId={novelId}
            nextChapterIndex={chapterIndex}
            onAdded={chapterAdded}
            onCancel={() => setAddingChapter(false)}
          />
        ) : showChapters ? (
          <ChapterList
            novelId={novelId}
            currentChapter={chapterIndex}
            onOpen={openChapter}
            onClose={() => setShowChapters(false)}
          />
        ) : pending ? (
          <ChapterPending
            key={`${novelId}:${pending.index}`}
            novelId={novelId}
            chapterIndex={pending.index}
            siteChapterNo={pending.siteChapterNo}
            onReady={() => goToChapter(pending.index)}
            onBack={() => {
              setPending(null);
              setShowChapters(true);
            }}
          />
        ) : noChapter && hasChapters ? (
          /* Chapters exist, just none finished translating. The list is the only useful view
             here — from it, opening a chapter queues it and shows it arriving. Rendering the
             add form instead (what this used to do) was a dead end on a novel that already
             held 177 chapters. Kept as its own branch rather than relying on showChapters so
             closing the list can't drop the reader back into that dead end. */
          <ChapterList
            novelId={novelId}
            currentChapter={chapterIndex}
            onOpen={openChapter}
            onClose={backToNovels}
          />
        ) : noChapter ? (
          <AddChapterForm
            novelId={novelId}
            nextChapterIndex={chapterIndex}
            onAdded={chapterAdded}
            onCancel={backToNovels}
          />
        ) : !progressLoaded ? (
          <p>Loading…</p>
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
            <button className="app-add-chapter" onClick={() => setAddingChapter(true)}>
              + Add chapter
            </button>
            {chapter && !showGlossary && <AskBox novelId={novelId} at={chapter.at} />}
          </>
        )}
      </div>
    </main>
  );
}
