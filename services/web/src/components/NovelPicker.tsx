import { useEffect, useState } from "react";
import { listNovels } from "../api";
import type { NovelSummary } from "../types";

interface Props {
  onSelect: (novelId: string) => void;
  onCreateNew: () => void;
}

export function NovelPicker({ onSelect, onCreateNew }: Props) {
  const [novels, setNovels] = useState<NovelSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listNovels()
      .then((response) => setNovels(response.novels))
      .catch((err) => setError(String(err)));
  }, []);

  return (
    <div className="novel-picker">
      <h1>Novels</h1>
      {error && <p className="novel-picker-error">Could not load novels: {error}</p>}
      {novels && novels.length === 0 && <p>No novels yet.</p>}
      {novels && novels.length > 0 && (
        <ul>
          {novels.map((novel) => (
            <li key={novel.id}>
              <button onClick={() => onSelect(novel.id)}>
                {novel.title} <span className="novel-picker-langs">({novel.source_lang} → {novel.target_lang})</span>
              </button>
            </li>
          ))}
        </ul>
      )}
      <button onClick={onCreateNew}>+ New novel</button>
    </div>
  );
}
