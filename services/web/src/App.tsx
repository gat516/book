import { useEffect, useState } from "react";
import { ApiError, getProgress, putProgress } from "./api";
import { AddChapterForm } from "./components/AddChapterForm";
import { AskBox } from "./components/AskBox";
import { ChapterList } from "./components/ChapterList";
import { ChapterPending } from "./components/ChapterPending";
import { GlossaryView } from "./components/GlossaryView";
import { NovelCreateForm } from "./components/NovelCreateForm";
import { NovelPicker } from "./components/NovelPicker";
import { ProgressControls } from "./components/ProgressControls";
import { ReaderPane } from "./components/ReaderPane";
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

export default function App() {
  const [novelId, setNovelId] = useState(novelIdFromLocation);
  const [creating, setCreating] = useState(false);
  const [chapterIndex, setChapterIndex] = useState(1);
  // Until stored progress has been resolved we don't know which chapter to open, and
  // mounting the reader on chapter 1 first would fetch a chapter the reader isn't on and
  // then visibly jump. Gate the reader on this instead.
  const [progressLoaded, setProgressLoaded] = useState(false);
  const [chapter, setChapter] = useState<ChapterResponse | null>(null);
  // Distinct from "loading" — set when the requested chapter doesn't exist (typically:
  // a brand-new novel with nothing pasted yet), so we can offer to add one instead of
  // showing an error or silently trying to render an empty reader pane.
  const [noChapter, setNoChapter] = useState(false);
  const [addingChapter, setAddingChapter] = useState(false);
  // A chapter the reader opened that the pipeline hasn't finished translating. Rendering
  // ChapterPending for it holds them here and polls until it's readable.
  const [pending, setPending] = useState<PendingChapter | null>(null);
  const [showGlossary, setShowGlossary] = useState(false);
  const [showChapters, setShowChapters] = useState(false);

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
    setAddingChapter(false);
    setPending(null);
    setShowGlossary(false);
    setShowChapters(false);
  }

  // Land on `index`, clearing whatever view was covering the reader.
  function goToChapter(index: number) {
    setChapterIndex(index);
    setChapter(null);
    setNoChapter(false);
    setPending(null);
    setShowChapters(false);
    setShowGlossary(false);
  }

  function chapterAdded(index: number) {
    setAddingChapter(false);
    setNoChapter(false);
    // A freshly pasted/scraped chapter is status='ingested' until the worker finishes it,
    // so route straight to the pending view rather than bouncing off an unreadable fetch.
    setPending({ index });
    setChapterIndex(index);
  }

  function chapterLoaded(response: ChapterResponse) {
    setChapter(response);
    setPending(null); // confirmed readable
  }

  async function openChapter(item: ChapterListItem) {
    if (item.status !== "done") {
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
          novelId={novelId}
          chapterIndex={pending.index}
          siteChapterNo={pending.siteChapterNo}
          onReady={() => goToChapter(pending.index)}
          onBack={() => {
            setPending(null);
            setShowChapters(true);
          }}
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
          {/* ReaderPane stays mounted (just hidden) rather than unmounting behind the
              glossary toggle, so switching back doesn't re-fetch/re-bootstrap progress. */}
          <div style={{ display: showGlossary ? "none" : "block" }}>
            <ReaderPane
              novelId={novelId}
              chapterIndex={chapterIndex}
              onChapterLoaded={chapterLoaded}
              onNoChapter={() => setNoChapter(true)}
            />
          </div>
          {showGlossary && chapter && <GlossaryView novelId={novelId} at={chapter.at} />}
          <ProgressControls
            novelId={novelId}
            chapterIndex={chapterIndex}
            hasNext={chapter?.has_next ?? false}
            onNavigate={goToChapter}
          />
          <button className="app-chapters" onClick={() => setShowChapters(true)}>
            All chapters
          </button>
          <button className="app-add-chapter" onClick={() => setAddingChapter(true)}>
            + Add chapter
          </button>
          {chapter && (
            <button className="app-toggle-glossary" onClick={() => setShowGlossary((v) => !v)}>
              {showGlossary ? "← Back to reading" : "Glossary"}
            </button>
          )}
          {chapter && !showGlossary && <AskBox novelId={novelId} at={chapter.at} />}
        </>
      )}
    </main>
  );
}
