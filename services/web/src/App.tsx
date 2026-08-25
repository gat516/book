import { useState } from "react";
import { AddChapterForm } from "./components/AddChapterForm";
import { AskBox } from "./components/AskBox";
import { NovelCreateForm } from "./components/NovelCreateForm";
import { NovelPicker } from "./components/NovelPicker";
import { ProgressControls } from "./components/ProgressControls";
import { ReaderPane } from "./components/ReaderPane";
import type { ChapterResponse } from "./types";

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

export default function App() {
  const [novelId, setNovelId] = useState(novelIdFromLocation);
  const [creating, setCreating] = useState(false);
  const [chapterIndex, setChapterIndex] = useState(1);
  const [chapter, setChapter] = useState<ChapterResponse | null>(null);
  // Distinct from "loading" — set when the requested chapter doesn't exist (typically:
  // a brand-new novel with nothing pasted yet), so we can offer to add one instead of
  // showing an error or silently trying to render an empty reader pane.
  const [noChapter, setNoChapter] = useState(false);
  const [addingChapter, setAddingChapter] = useState(false);
  // Set right after a successful paste. A chapter starts life as status="ingested" and
  // only becomes readable once the pipeline worker processes it to "done" — until then
  // ReaderPane's onNoChapter fires exactly the same way it would for a truly empty novel.
  // Tracking this separately lets that specific case say "still processing" instead of
  // bouncing straight back to a blank "add a chapter" form, which would look like the
  // paste silently failed.
  const [justAdded, setJustAdded] = useState<number | null>(null);

  function chooseNovel(id: string) {
    setNovelInLocation(id);
    setNovelId(id);
    setCreating(false);
    setChapterIndex(1);
    setChapter(null);
    setNoChapter(false);
    setAddingChapter(false);
    setJustAdded(null);
  }

  function backToNovels() {
    setNovelInLocation(null);
    setNovelId(null);
    setChapter(null);
    setNoChapter(false);
    setAddingChapter(false);
    setJustAdded(null);
  }

  function chapterAdded(index: number) {
    setAddingChapter(false);
    setNoChapter(false);
    setJustAdded(index);
    setChapterIndex(index);
  }

  function chapterLoaded(response: ChapterResponse) {
    setChapter(response);
    setJustAdded(null); // confirmed readable — no longer "might still be processing"
  }

  function startAddingChapter() {
    setJustAdded(null);
    setAddingChapter(true);
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
      ) : noChapter && justAdded === chapterIndex ? (
        <div className="chapter-processing">
          <p>Chapter {chapterIndex} was added and is being processed — it isn't readable yet.</p>
          <p className="chapter-processing-hint">
            Run the pipeline worker (see CLAUDE.md) if it isn't already running, then check again.
          </p>
          <button onClick={() => setNoChapter(false)}>Check again</button>
        </div>
      ) : noChapter ? (
        <AddChapterForm
          novelId={novelId}
          nextChapterIndex={chapterIndex}
          onAdded={chapterAdded}
          onCancel={backToNovels}
        />
      ) : (
        <>
          <ReaderPane
            novelId={novelId}
            chapterIndex={chapterIndex}
            onChapterLoaded={chapterLoaded}
            onNoChapter={() => setNoChapter(true)}
          />
          <ProgressControls
            novelId={novelId}
            chapterIndex={chapterIndex}
            hasNext={chapter?.has_next ?? false}
            onNavigate={setChapterIndex}
          />
          <button className="app-add-chapter" onClick={startAddingChapter}>
            + Add chapter
          </button>
          {chapter && <AskBox novelId={novelId} at={chapter.at} />}
        </>
      )}
    </main>
  );
}
