import { useState } from "react";
import { putProgress } from "../api";

interface Props {
  novelId: string;
  chapterIndex: number;
  hasNext: boolean;
  onNavigate: (chapterIndex: number) => void;
}

export function ProgressControls({ novelId, chapterIndex, hasNext, onNavigate }: Props) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function next() {
    setPending(true);
    setError(null);
    try {
      // Finishing this chapter advances progress to (at least) it; everything re-gates
      // from here (PLAN.md §5.5). `has_next` is an existence check decoupled from
      // progress, not "chapterIndex+1 <= progress" — see reader-api's GetChapter.
      await putProgress(novelId, chapterIndex);
      onNavigate(chapterIndex + 1);
    } catch (err) {
      setError(String(err));
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="progress-controls">
      <button disabled={chapterIndex <= 0} onClick={() => onNavigate(chapterIndex - 1)}>
        Prev
      </button>
      <span>Chapter {chapterIndex}</span>
      <button disabled={!hasNext || pending} onClick={next}>
        {pending ? "…" : "Next"}
      </button>
      {error && <p className="progress-controls-error">{error}</p>}
    </div>
  );
}
