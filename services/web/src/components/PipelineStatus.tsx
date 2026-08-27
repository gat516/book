import { useCallback, useEffect, useState } from "react";
import { getPipelineStatus } from "../api";
import type { PipelineStatusResponse } from "../types";
import { usePolling } from "../usePolling";

interface Props {
  novelId: string;
  // Fired whenever the set of in-flight chapters changes, so a parent can refresh itself
  // on real pipeline progress instead of running a second timer of its own.
  onProgress?: () => void;
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
export function PipelineStatus({ novelId, onProgress }: Props) {
  const [status, setStatus] = useState<PipelineStatusResponse | null>(null);
  const [unreachable, setUnreachable] = useState(false);

  const poll = useCallback(async () => {
    try {
      const latest = await getPipelineStatus(novelId);
      setStatus((previous) => {
        // Only notify on an actual change in what's being worked on. Firing every tick
        // would make the parent re-fetch on a timer again, which is the pattern this is
        // meant to replace.
        const before = previous?.in_flight.map((c) => c.chapter_index).join(",") ?? "";
        const after = latest.in_flight.map((c) => c.chapter_index).join(",");
        if (previous !== null && before !== after) onProgress?.();
        return latest;
      });
      setUnreachable(false);
    } catch {
      setUnreachable(true);
    }
  }, [novelId, onProgress]);

  useEffect(() => {
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
            <strong>Chapter {item.chapter_index}:</strong> {describe(item.stage)} — {elapsed(item.elapsed_secs)} so far
          </p>
        ))
      ) : (
        <p>
          {status.pending > 0
            ? // Queued but nothing claimed almost always means the worker isn't running —
              // the queue does not drain itself, and this is the exact state that looked
              // like a silent failure before this view existed.
              `${status.pending} chapter(s) queued, but the pipeline worker isn't processing anything. Is it running? (see CLAUDE.md)`
            : "Pipeline idle — nothing queued."}
        </p>
      )}
      {working && status.pending > 0 && <p className="pipeline-status-queue">{status.pending} more queued.</p>}
    </div>
  );
}
