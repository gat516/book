import { useCallback, useEffect, useState } from "react";
import { ApiError, cancelScrape, getScrapeStatus, previewScrape, startScrape } from "../api";
import type { ScrapeJobView, ScrapePreview } from "../types";
import { usePolling } from "../usePolling";

interface Props {
  novelId: string;
  onDone: () => void; // called once a job reaches a terminal state, to refresh the reader
}

// 2s was far tighter than the thing it watches: the scraper is rate-limited to well under
// one page per second, so a status poll at that rate mostly returned an unchanged row
// while competing with the other live views on the page.
const POLL_INTERVAL_MS = 5000;

// Any site can be tried: hosts without an adapter of their own are read by the generic
// reader (scraper/generic.go), which works out where the chapter and the next link are.
// That is a guess, so "Check this page" previews one page first -- what it extracted, how
// much of it, and where it would go next -- before a scrape runs on the strength of it.
// Mode only controls whether fetched text is treated as already-translated.

// Acknowledging responsibility for a source is a one-time thing per browser, not a
// per-scrape nag; the notice itself stays visible either way.
const ACK_KEY = "novel-engine:source-responsibility";

function storedAck(): boolean {
  try {
    return localStorage.getItem(ACK_KEY) === "yes";
  } catch {
    return false; // private mode or blocked storage: ask again rather than assume
  }
}
export function ScrapeForm({ novelId, onDone }: Props) {
  const [startURL, setStartURL] = useState("");
  const [mode, setMode] = useState<"translate" | "bootstrap">("translate");
  const [job, setJob] = useState<ScrapeJobView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [checking, setChecking] = useState(false);
  const [preview, setPreview] = useState<ScrapePreview | null>(null);
  const [acked, setAcked] = useState(storedAck);

  useEffect(() => {
    // Pick up an already-running job for this novel (e.g. after a page reload) rather
    // than assuming a fresh mount means no job exists.
    getScrapeStatus(novelId)
      .then(setJob)
      .catch((reason) => {
        // A 404 is the normal first-use state. Do not turn an API/database outage into
        // "no job yet"; status is unknown and should be visible to the reader.
        if (!(reason instanceof ApiError && reason.status === 404)) setError(errorMessage(reason));
      });
  }, [novelId]);

  const poll = useCallback(async () => {
    try {
      const latest = await getScrapeStatus(novelId);
      setJob(latest);
      if (latest.status !== "pending" && latest.status !== "running") onDone();
    } catch (err) {
      setError(errorMessage(err));
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

  function acknowledge(next: boolean) {
    setAcked(next);
    try {
      localStorage.setItem(ACK_KEY, next ? "yes" : "no");
    } catch { /* storage is a convenience here; the checkbox still governs this session */ }
  }

  async function check() {
    if (!startURL.trim()) return;
    setChecking(true);
    setError(null);
    setPreview(null);
    try {
      const result = await previewScrape(novelId, startURL.trim());
      setPreview(result);
      if (result.suggested_mode) setMode(result.suggested_mode);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setChecking(false);
    }
  }

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
      setError(err instanceof ApiError && err.status === 409 ? "A scrape is already running for this novel." : errorMessage(err));
    } finally {
      setPending(false);
    }
  }

  async function cancel() {
    try {
      await cancelScrape(novelId);
    } catch (err) {
      setError(errorMessage(err));
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
        {error && <p role="alert" className="scrape-form-error">
          Could not refresh scrape status: {error} <button type="button" onClick={() => { setError(null); void poll(); }}>Retry</button>
        </p>}
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
            onChange={(e) => { setStartURL(e.target.value); setPreview(null); }}
            placeholder="https://example.com/novel/x/chapter-1"
            required
          />
        </label>
        <div className="scrape-check">
          <button type="button" onClick={() => void check()} disabled={checking || !startURL.trim()}>
            {checking ? "Reading the page…" : "Check this page"}
          </button>
          <small>Reads one page and shows what would be saved.</small>
        </div>
        {preview && <ScrapePreviewPanel preview={preview} />}
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
        <label className="scrape-ack">
          <input type="checkbox" checked={acked} onChange={(event) => acknowledge(event.target.checked)} />
          <span>
            I have the right to read this source and I am responsible for how this copy is used.
            Chapters stay in my own library; the scraper follows each site's robots.txt and fetches
            slowly.
          </span>
        </label>
        <button type="submit" className="btn-primary" disabled={pending || !startURL.trim() || !acked}>
          {pending ? "Starting…" : "Start scrape"}
        </button>
      </form>
      {error && <p className="scrape-form-error">{error}</p>}
    </div>
  );
}

function errorMessage(reason: unknown): string {
  return reason instanceof Error ? reason.message : String(reason);
}

// What the preview found, in the order it answers a reader's questions: did it read the
// right thing, how much of it, and where would it go next.
function ScrapePreviewPanel({ preview }: { preview: ScrapePreview }) {
  if (preview.error) {
    return <div className="scrape-preview is-bad" role="alert">
      <strong>{preview.host || "That URL"} didn’t read as a chapter.</strong>
      <p>{preview.error}</p>
    </div>;
  }
  return <div className="scrape-preview">
    <p className="scrape-preview-head">
      <strong>{preview.title || "Untitled page"}</strong>
      <span>{preview.text_chars.toLocaleString()} characters · {preview.paragraphs} paragraph{preview.paragraphs === 1 ? "" : "s"}</span>
    </p>
    <blockquote>{preview.excerpt}…</blockquote>
    <ul className="scrape-preview-facts">
      <li>{preview.reader === "built-in"
        ? `Read by the adapter written for ${preview.host}.`
        : `Read by the generic reader, which works out this site's layout per page.`}</li>
      <li>{preview.next_url
        ? (preview.continues
          ? "The next link continues this same chapter; both pages become one chapter."
          : "The next link goes to the following chapter.")
        : "No next link found, so a scrape would stop after this page."}</li>
      <li>{preview.suggested_mode === "bootstrap"
        ? "Reads as already translated."
        : "Reads as source-language text to translate."}</li>
    </ul>
  </div>;
}
