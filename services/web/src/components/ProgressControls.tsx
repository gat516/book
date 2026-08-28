import { useState } from "react";

interface Props {
  chapterIndex: number;
  hasNext: boolean;
  onNavigate: (chapterIndex: number) => Promise<void>;
}

export function ProgressControls({ chapterIndex, hasNext, onNavigate }: Props) {
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

  return (
    <div className="progress-controls">
      <button disabled={chapterIndex <= 1 || pending} onClick={() => navigate(chapterIndex - 1)}>
        Prev
      </button>
      <span>Chapter {chapterIndex}</span>
      <button disabled={!hasNext || pending} onClick={() => navigate(chapterIndex + 1)}>
        {pending ? "…" : "Next"}
      </button>
      {error && <p className="progress-controls-error">{error}</p>}
    </div>
  );
}
