import { useEffect, useRef, useState } from "react";
import { ApiError, cancelScrape, getScrapeStatus, startScrape } from "../api";
import type { ScrapeJobView } from "../types";

interface Props {
  novelId: string;
  onDone: () => void; // called once a job reaches a terminal state, to refresh the reader
}

const POLL_INTERVAL_MS = 2000;

// Two real sources this was built and tested against (PLAN.md Phase N5):
//   - an already-translated site (mode "bootstrap" — fetched text needs no LLM call)
//   - a raw source-language site (mode "translate" — the pipeline machine-translates it)
// The scraper infers which site a URL belongs to from its host; mode only controls
// whether fetched text is treated as already-translated or not.
export function ScrapeForm({ novelId, onDone }: Props) {
  const [startURL, setStartURL] = useState("");
  const [mode, setMode] = useState<"translate" | "bootstrap">("translate");
  const [job, setJob] = useState<ScrapeJobView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    // Pick up an already-running job for this novel (e.g. after a page reload) rather
    // than assuming a fresh mount means no job exists.
    getScrapeStatus(novelId)
      .then((existing) => {
        setJob(existing);
        if (existing.status === "pending" || existing.status === "running") startPolling();
      })
      .catch(() => {
        /* no job yet for this novel — fine, the form below is the way to start one */
      });
    return () => stopPolling();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [novelId]);

  function stopPolling() {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }

  function startPolling() {
    stopPolling();
    pollRef.current = setInterval(async () => {
      try {
        const latest = await getScrapeStatus(novelId);
        setJob(latest);
        if (latest.status !== "pending" && latest.status !== "running") {
          stopPolling();
          onDone();
        }
      } catch (err) {
        stopPolling();
        setError(String(err));
      }
    }, POLL_INTERVAL_MS);
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!startURL.trim()) return;
    setPending(true);
    setError(null);
    try {
      await startScrape(novelId, { start_url: startURL, mode });
      const initial = await getScrapeStatus(novelId);
      setJob(initial);
      startPolling();
    } catch (err) {
      setError(err instanceof ApiError && err.status === 409 ? "A scrape is already running for this novel." : String(err));
    } finally {
      setPending(false);
    }
  }

  async function cancel() {
    try {
      await cancelScrape(novelId);
    } catch (err) {
      setError(String(err));
    }
  }

  const active = job && (job.status === "pending" || job.status === "running");

  if (active) {
    return (
      <div className="scrape-status">
        <p>
          Scraping <code>{job.start_url}</code> — {job.chapters_fetched} chapter(s) fetched.
        </p>
        <button onClick={cancel} disabled={job.cancel_requested}>
          {job.cancel_requested ? "Cancelling…" : "Cancel"}
        </button>
      </div>
    );
  }

  return (
    <div className="scrape-form">
      {job && job.status !== "pending" && job.status !== "running" && (
        <p className="scrape-status-summary">
          Last scrape of <code>{job.start_url}</code>: <strong>{job.status}</strong>
          {job.status === "done" && `, ${job.chapters_fetched} chapter(s) fetched`}
          {job.last_error && ` — ${job.last_error}`}
        </p>
      )}
      <form onSubmit={submit}>
        <label>
          Chapter URL
          <input
            type="url"
            value={startURL}
            onChange={(e) => setStartURL(e.target.value)}
            placeholder="https://example.com/novel/x/chapter-1"
            required
          />
        </label>
        <fieldset>
          <legend>This site's text is</legend>
          <label>
            <input type="radio" checked={mode === "translate"} onChange={() => setMode("translate")} /> Original
            language (will be machine-translated)
          </label>
          <label>
            <input type="radio" checked={mode === "bootstrap"} onChange={() => setMode("bootstrap")} /> Already
            translated
          </label>
        </fieldset>
        <button type="submit" disabled={pending || !startURL.trim()}>
          {pending ? "Starting…" : "Start scrape"}
        </button>
      </form>
      {error && <p className="scrape-form-error">{error}</p>}
    </div>
  );
}
