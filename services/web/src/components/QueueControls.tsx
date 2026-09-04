import { useCallback, useEffect, useRef, useState } from "react";
import { getQueueControl, updateQueueControl } from "../api";
import type { QueueControl, QueueMode } from "../api";
import { usePolling } from "../usePolling";
import { describeStage } from "./PipelineStatus";

export function QueueControls({ novelId }: { novelId: string | null }) {
  const [queue, setQueue] = useState<QueueControl | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const changing = useRef(false);
  const generation = useRef(0);

  const refresh = useCallback(async () => {
    if (changing.current) return;
    const version = generation.current;
    try {
      const result = await getQueueControl();
      if (version !== generation.current) return;
      setQueue(result);
      setError(null);
    } catch (err) {
      if (version === generation.current) setError(String(err));
    }
  }, []);

  const apply = useCallback(async (patch: { mode?: QueueMode; focus_novel_id?: string; reason?: string }) => {
    const version = ++generation.current;
    changing.current = true;
    setBusy(true);
    try {
      const result = await updateQueueControl(patch);
      if (version === generation.current) { setQueue(result); setError(null); }
    } catch (err) {
      if (version === generation.current) setError(String(err));
    } finally {
      if (version === generation.current) { changing.current = false; setBusy(false); }
    }
  }, []);

  useEffect(() => {
    // Focus only on navigation or returning to a visible tab, never on each poll.
    // No focus write on unmount: it could erase the book just selected in another tab.
    const focus = () => {
      if (novelId && document.visibilityState !== "hidden") {
        void apply({ focus_novel_id: novelId });
      }
    };
    if (novelId) focus(); else void refresh();
    window.addEventListener("focus", focus);
    document.addEventListener("visibilitychange", focus);
    return () => {
      window.removeEventListener("focus", focus);
      document.removeEventListener("visibilitychange", focus);
    };
  }, [novelId, apply, refresh]);

  usePolling(refresh, 8000, true);
  const focused = queue?.books.find((book) => book.novel_id === queue.focus_novel_id);
  const active = queue?.books.flatMap((book) => book.in_flight.map((chapter) => ({ book, chapter }))) ?? [];

  return <section className="queue-controls" aria-label="Library processing queue">
    <h2>Processing queue</h2>
    <label>Work on {" "}
      <select aria-label="Queue mode" value={queue?.mode ?? "all"} disabled={busy || !queue}
        onChange={(event) => {
          const mode = event.target.value as QueueMode;
          const reason = mode === "paused"
            ? "Paused from the reader's processing queue controls"
            : `Changed to ${mode} from the reader's processing queue controls`;
          void apply({ mode, reason, ...(novelId ? { focus_novel_id: novelId } : {}) });
        }}>
        <option value="all">Open book first, then background books</option>
        <option value="focused">Only the focused book</option>
        <option value="paused">Paused — finish current work first</option>
      </select>
    </label>
    <p role="status">
      {queue?.mode === "paused" ? "Paused: no new chapter or background graph work will start." :
        focused ? `Priority: ${focused.title} (${focused.novel_id.slice(0, 8)}).` : "Open a book to give it priority."}
      {busy && " Saving…"}
    </p>
    {queue?.mode === "paused" && (
      <p>
        {queue.mode_changed_at
          ? <>Paused {new Date(queue.mode_changed_at).toLocaleString()} by {queue.mode_changed_by || "an unknown reader"}{queue.mode_reason ? ` — ${queue.mode_reason}.` : "."}</>
          : "This pause was set before pause auditing was added, so its original reason was not recorded."}
      </p>
    )}
    {queue?.mode === "paused" && focused && <p>Focused after resume: {focused.title} ({focused.novel_id.slice(0, 8)}).</p>}
    {queue?.mode === "focused" && <p>Other books stay queued until you switch books or enable background work.</p>}
    {active.map(({ book, chapter }) => <p key={`${book.novel_id}:${chapter.chapter_index}`}>
      Running: <strong>{book.title}</strong> ({book.novel_id.slice(0, 8)}), chapter {chapter.chapter_index} — {describeStage(chapter.stage)}.
    </p>)}
    <p className="queue-note">Switching books takes effect after current work finishes. Saved translations and queued chapters are kept. These controls are shared across tabs; the last book you focus wins. Scraping is separate.</p>
    {queue?.mode === "paused" && <button disabled={busy} onClick={() => void apply({
      mode: "all",
      reason: "Resumed from the reader's processing queue controls",
      ...(novelId ? { focus_novel_id: novelId } : {}),
    })}>Resume processing</button>}
    {novelId && queue?.focus_novel_id !== novelId && <button disabled={busy} onClick={() => void apply({ focus_novel_id: novelId })}>Prioritize this book</button>}
    {queue && queue.books.length > 0 && <details>
      <summary>Queued by book ({queue.books.reduce((sum, book) => sum + book.pending, 0)} chapters)</summary>
      <ul>{queue.books.map((book) => <li key={book.novel_id}>
        {book.title} ({book.novel_id.slice(0, 8)}) — {book.pending} queued
        {book.novel_id === queue.focus_novel_id ? " · focused" : queue.mode === "focused" ? " · waiting" : ""}
      </li>)}</ul>
    </details>}
    {error && <p role="alert">Queue controls unavailable: {error} <button onClick={() => void (novelId ? apply({ focus_novel_id: novelId }) : refresh())}>Retry</button></p>}
  </section>;
}
