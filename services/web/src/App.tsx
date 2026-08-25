import { useState } from "react";
import { AskBox } from "./components/AskBox";
import { ProgressControls } from "./components/ProgressControls";
import { ReaderPane } from "./components/ReaderPane";
import type { ChapterResponse } from "./types";

// No novel-picker UI in Milestone 1 scope (PLAN.md §11.7: one page). `?novel=` is
// preferred over a hardcoded default so the app is shareable/demoable via URL; falls
// back to VITE_NOVEL_ID so it isn't blank with no param.
function novelIdFromLocation(): string | null {
  const fromQuery = new URLSearchParams(window.location.search).get("novel");
  return fromQuery ?? (import.meta.env.VITE_NOVEL_ID as string | undefined) ?? null;
}

export default function App() {
  const [novelId] = useState(novelIdFromLocation);
  const [chapterIndex, setChapterIndex] = useState(1);
  const [chapter, setChapter] = useState<ChapterResponse | null>(null);

  if (!novelId) {
    return (
      <p className="app-error">
        No novel selected. Add <code>?novel=&lt;id&gt;</code> to the URL.
      </p>
    );
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
