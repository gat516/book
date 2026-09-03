import { useState } from "react";

interface Props {
  chapterIndex: number;
  hasNext: boolean;
  onNavigate: (chapterIndex: number) => Promise<void>;
  sourceURL?: string;
  onFindMore?: () => Promise<void>;
}

export function ProgressControls({ chapterIndex, hasNext, onNavigate, sourceURL, onFindMore }: Props) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function navigate(index: number) {
    setPending(true);
    setError(null);
    try {
      await onNavigate(index);
    } catch (err) {
      setError(String(err));
    } finally {
      setPending(false);
    }
  }

  async function findMore() {
    if (!onFindMore) return;
    setPending(true);
    setError(null);
    try {
      await onFindMore();
    } catch (err) {
      setError(String(err));
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="progress-controls">
      <button disabled={chapterIndex <= 1 || pending} onClick={() => navigate(chapterIndex - 1)}>
        Prev
      </button>
      <span>Chapter {chapterIndex}</span>
      <button
        disabled={pending || (!hasNext && (!sourceURL || !onFindMore))}
        onClick={() => hasNext ? navigate(chapterIndex + 1) : void findMore()}
      >
        {pending ? "…" : hasNext ? "Next" : "Find next chapters"}
      </button>
      {error && <p className="progress-controls-error">{error}</p>}
    </div>
  );
}
