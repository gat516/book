import { useCallback, useEffect, useState } from "react";
import { ApiError, cancelScrape, getScrapeStatus, startScrape } from "../api";
import type { ScrapeJobView } from "../types";
import { usePolling } from "../usePolling";

interface Props {
  novelId: string;
  onDone: () => void; // called once a job reaches a terminal state, to refresh the reader
}

// 2s was far tighter than the thing it watches: the scraper is rate-limited to well under
// one page per second, so a status poll at that rate mostly returned an unchanged row
// while competing with the other live views on the page.
const POLL_INTERVAL_MS = 5000;

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

  useEffect(() => {
    // Pick up an already-running job for this novel (e.g. after a page reload) rather
    // than assuming a fresh mount means no job exists.
    getScrapeStatus(novelId)
      .then(setJob)
      .catch(() => {
        /* no job yet for this novel — fine, the form below is the way to start one */
      });
  }, [novelId]);

  const poll = useCallback(async () => {
    try {
      const latest = await getScrapeStatus(novelId);
      setJob(latest);
      if (latest.status !== "pending" && latest.status !== "running") onDone();
    } catch (err) {
      setError(String(err));
    }
    // onDone is recreated by the parent on every render; depending on it here would
    // rebuild this callback constantly. usePolling holds the callback in a ref, so the
    // interval is unaffected either way.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [novelId]);

  // Driven by the job's own state rather than an imperative start/stop pair: a finished,
  // cancelled or errored job simply isn't "active", so the timer stops on its own and
  // can't be left running by a missed stopPolling() call.
  const active = job !== null && (job.status === "pending" || job.status === "running");
  usePolling(poll, POLL_INTERVAL_MS, active && error === null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!startURL.trim()) return;
    setPending(true);
    setError(null);
    try {
      await startScrape(novelId, { start_url: startURL, mode });
      // Setting the job to a running status is what arms the poll now — no separate
      // start call to forget.
      setJob(await getScrapeStatus(novelId));
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
