import { useState } from "react";
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

function selectNovel(novelId: string) {
  const url = new URL(window.location.href);
  url.searchParams.set("novel", novelId);
  window.history.pushState({}, "", url);
}

export default function App() {
  const [novelId, setNovelId] = useState(novelIdFromLocation);
  const [creating, setCreating] = useState(false);
  const [chapterIndex, setChapterIndex] = useState(1);
  const [chapter, setChapter] = useState<ChapterResponse | null>(null);

  function chooseNovel(id: string) {
    selectNovel(id);
    setNovelId(id);
    setCreating(false);
  }

  if (!novelId) {
    if (creating) {
      return <NovelCreateForm onCreated={chooseNovel} onCancel={() => setCreating(false)} />;
    }
    return <NovelPicker onSelect={chooseNovel} onCreateNew={() => setCreating(true)} />;
  }

  return (
    <main className="app">
      <ReaderPane novelId={novelId} chapterIndex={chapterIndex} onChapterLoaded={setChapter} />
      <ProgressControls
        novelId={novelId}
        chapterIndex={chapterIndex}
        hasNext={chapter?.has_next ?? false}
        onNavigate={setChapterIndex}
      />
      {chapter && <AskBox novelId={novelId} at={chapter.at} />}
    </main>
  );
}
