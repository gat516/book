import { useEffect, useState } from "react";
import { deleteNovel, listNovels } from "../api";
import type { NovelSummary } from "../types";

interface Props {
  onSelect: (novelId: string) => void;
  onCreateNew: () => void;
}

export function NovelPicker({ onSelect, onCreateNew }: Props) {
  const [novels, setNovels] = useState<NovelSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Two-step delete: the first click arms this novel, the second commits. A confirm()
  // dialog would do the same job, but deleting a novel throws away every chapter and the
  // whole graph built from it, so the confirmation names what is about to go.
  const [armed, setArmed] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);

  useEffect(() => {
    listNovels()
      .then((response) => setNovels(response.novels))
      .catch((err) => setError(String(err)));
  }, []);

  async function confirmDelete(novel: NovelSummary) {
    setDeleting(novel.id);
    setError(null);
    try {
      await deleteNovel(novel.id);
      setNovels((current) => (current ?? []).filter((n) => n.id !== novel.id));
      setArmed(null);
    } catch (err) {
      setError(`Could not delete ${novel.title}: ${err}`);
    } finally {
      setDeleting(null);
    }
  }

  return (
    <div className="novel-picker">
      <h1>Novels</h1>
      {error && <p className="novel-picker-error">{error}</p>}
      {novels && novels.length === 0 && <p>No novels yet.</p>}
      {novels && novels.length > 0 && (
        <ul>
          {novels.map((novel) => (
            <li key={novel.id}>
              <button onClick={() => onSelect(novel.id)}>
                {novel.title} <span className="novel-picker-langs">({novel.source_lang} → {novel.target_lang})</span>
              </button>
              {armed === novel.id ? (
                <span className="novel-picker-confirm">
                  Delete {novel.title} and all its chapters, translations and knowledge? This cannot be undone.
                  <button
                    className="novel-picker-delete"
                    disabled={deleting === novel.id}
                    onClick={() => void confirmDelete(novel)}
                  >
                    {deleting === novel.id ? "Deleting…" : "Delete permanently"}
                  </button>
                  <button disabled={deleting === novel.id} onClick={() => setArmed(null)}>
                    Cancel
                  </button>
                </span>
              ) : (
                <button className="novel-picker-delete" onClick={() => setArmed(novel.id)}>
                  Delete
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
      <button onClick={onCreateNew}>+ New novel</button>
    </div>
  );
}
