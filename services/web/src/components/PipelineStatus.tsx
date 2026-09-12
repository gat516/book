import { useCallback, useEffect, useRef, useState } from "react";
import { getPipelineStatus } from "../api";
import type { PipelineStatusResponse } from "../types";
import { usePolling } from "../usePolling";
import { describeStage, stageProgress } from "../pipelineStages";
export { describeStage, stageProgress } from "../pipelineStages";

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
  const [unreachable, setUnreachable] = useState<string | null>(null);
  const [polledAt, setPolledAt] = useState(Date.now());
  const [clock, setClock] = useState(Date.now());
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
      const now = Date.now();
      setPolledAt(now);
      setClock(now);
      callbacks.current.onStatus?.(latest);
      setUnreachable(null);
    } catch (reason) {
      setUnreachable(errorMessage(reason));
      callbacks.current.onStatus?.(null);
    }
  }, [novelId]);

  useEffect(() => {
    previous.current = null;
    setStatus(null);
    setUnreachable(null);
    callbacks.current.onStatus?.(null);
    poll();
  }, [poll]);

  // Poll fast while there is something to report, slowly otherwise — but never stop, so
  // work starting after an idle reading is still noticed (see IDLE_POLL_INTERVAL_MS).
  const busy = status === null || status.in_flight.length > 0 || status.pending > 0;
  usePolling(poll, busy ? POLL_INTERVAL_MS : IDLE_POLL_INTERVAL_MS, unreachable === null);

  // Keep elapsed time visibly moving between network polls. Long local-model stages can
  // take minutes; a ticking timer reassures the reader that the status view itself is live.
  useEffect(() => {
    if (!status?.in_flight.length) return;
    const timer = window.setInterval(() => setClock(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [status?.in_flight.length]);

  if (unreachable) return <p className="pipeline-status" role="alert">
    Pipeline status unavailable: {unreachable} <button type="button" onClick={() => { setUnreachable(null); void poll(); }}>Retry</button>
  </p>;
  if (!status) return null;

  const working = status.in_flight.length > 0;
  const sincePoll = Math.max(0, Math.floor((clock - polledAt) / 1000));
  return (
    <div className={`pipeline-status ${working ? "pipeline-status-working" : ""}`} aria-live="polite">
      {working ? (
        status.in_flight.map((item) => {
          const { step, total } = stageProgress(item.stage);
          return <div className="pipeline-status-job" key={item.chapter_index}>
            <p className="pipeline-status-heading">
              <span className="pipeline-status-live-dot" aria-hidden="true" />
              <strong>Actively processing chapter {item.chapter_index}</strong>
            </p>
            <p>
              {describeStage(item.stage)} · step {step} of {total} · {elapsed(item.stage_elapsed_secs + sincePoll)} in this step · {elapsed(item.elapsed_secs + sincePoll)} total
            </p>
            <progress value={step} max={total} aria-label={`Chapter ${item.chapter_index} pipeline progress`} />
          </div>;
        })
      ) : (
        <p>
          {status.queue_mode === "paused" && status.pending_for_novel > 0
            ? `Queue paused — ${status.pending_for_novel} chapter(s) are intentionally waiting. Resume work in Processing queue.`
            : !status.worker_online && status.pending_for_novel > 0
            ? `Worker offline — ${status.pending_for_novel} chapter(s) from this book are queued but cannot start.`
            : status.pending_for_novel > 0
              ? `Worker online — ${status.pending_for_novel} chapter(s) from this book are waiting for their turn.`
              : status.pending > 0
                ? `Worker ${status.worker_online ? "online" : "offline"}. ${status.pending} job(s) from other books are queued.`
            : "Pipeline idle — nothing queued."}
        </p>
      )}
      {working && status.pending_for_novel > 0 && <p className="pipeline-status-queue">
        {status.pending_for_novel} more from this book queued · {status.pending} queued across the library.
      </p>}
    </div>
  );
}

function errorMessage(reason: unknown): string {
  return reason instanceof Error ? reason.message : String(reason);
}
