import { useCallback, useEffect, useRef, useState } from "react";
import { getPipelineStatus } from "../api";
import type { PipelineStatusResponse } from "../types";
import { usePolling } from "../usePolling";

interface Props {
  novelId: string;
  // Fired whenever the set of in-flight chapters changes, so a parent can refresh itself
  // on real pipeline progress instead of running a second timer of its own.
  onProgress?: () => void;
  onStatus?: (status: PipelineStatusResponse | null) => void;
}

// 3s was needlessly aggressive for work that takes minutes per chapter, and it was one of
// several timers running on the same page.
const POLL_INTERVAL_MS = 8000;
// Idle still polls, just rarely. Disabling it outright was a latch with no way out: the
// first poll after opening a chapter can legitimately see an empty queue (the request that
// queues it hasn't landed yet), and stopping there meant nothing ever looked again — the
// view claimed "Pipeline idle" while the worker was minutes into translating that very
// chapter. Backing off keeps an idle page cheap without making it blind.
const IDLE_POLL_INTERVAL_MS = 20000;

// Human labels for the pipeline's stage names (worker.py's DEFAULT_STAGES). The raw names
// are internal jargon; "resolve" means nothing to someone waiting on a chapter.
const STAGE_LABELS: Record<string, string> = {
  chunk: "Splitting into chunks",
  character_names: "Checking character names",
  scan: "Scanning for known names",
  resolve: "Identifying characters and places",
  translate: "Translating",
  display_scan: "Marking names in the translation",
  state: "Extracting facts",
  graph_write: "Saving to the knowledge graph",
};

function describe(stage?: string): string {
  if (!stage) return "Starting…";
  return STAGE_LABELS[stage] ?? stage;
}

function elapsed(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

// A live view of what the pipeline worker is doing. This exists because a chapter can sit
// unreadable for many minutes with no outward sign: a single TRANSLATE call against a
// local model is one long HTTP request, and chapter.status stays "ingested" until every
// stage has finished — so "still working" and "worker is dead" look identical without it.
export function PipelineStatus({ novelId, onProgress, onStatus }: Props) {
  const [status, setStatus] = useState<PipelineStatusResponse | null>(null);
  const [unreachable, setUnreachable] = useState(false);
  const previous = useRef<string | null>(null);
  const callbacks = useRef({ onProgress, onStatus });
  useEffect(() => { callbacks.current = { onProgress, onStatus }; }, [onProgress, onStatus]);

  const poll = useCallback(async () => {
    try {
      const latest = await getPipelineStatus(novelId);
      const signature = JSON.stringify([latest.pending, latest.in_flight.map((c) => [c.chapter_index, c.stage])]);
      if (previous.current !== null && previous.current !== signature) callbacks.current.onProgress?.();
      previous.current = signature;
      setStatus(latest);
      callbacks.current.onStatus?.(latest);
      setUnreachable(false);
    } catch {
      setUnreachable(true);
      callbacks.current.onStatus?.(null);
    }
  }, [novelId]);

  useEffect(() => {
    previous.current = null;
    setStatus(null);
    callbacks.current.onStatus?.(null);
    poll();
  }, [poll]);

  // Poll fast while there is something to report, slowly otherwise — but never stop, so
  // work starting after an idle reading is still noticed (see IDLE_POLL_INTERVAL_MS).
  const busy = status === null || status.in_flight.length > 0 || status.pending > 0;
  usePolling(poll, busy ? POLL_INTERVAL_MS : IDLE_POLL_INTERVAL_MS, true);

  if (unreachable) return <p className="pipeline-status">Pipeline status unavailable.</p>;
  if (!status) return null;

  const working = status.in_flight.length > 0;

  return (
    <div className="pipeline-status">
      {working ? (
        status.in_flight.map((item) => (
          <p key={item.chapter_index}>
            <strong>Chapter {item.chapter_index}:</strong> {describe(item.stage)} — {elapsed(item.elapsed_secs)} total processing time
          </p>
        ))
      ) : (
        <p>
          {status.pending > 0
            ? // Pending is library-wide, while in_flight is scoped to this novel.
              // Another novel may be active: absence here does not prove a dead worker.
              `No chapter from this novel is processing. ${status.pending} job(s) queued across the library.`
            : "Pipeline idle — nothing queued."}
        </p>
      )}
      {working && status.pending > 0 && <p className="pipeline-status-queue">{status.pending} more queued across the library.</p>}
    </div>
  );
}
