import { useEffect, useState } from "react";
import { getPipelineStatus } from "../api";
import type { PipelineStatusResponse } from "../types";

interface Props {
  novelId: string;
}

const POLL_INTERVAL_MS = 3000;

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
export function PipelineStatus({ novelId }: Props) {
  const [status, setStatus] = useState<PipelineStatusResponse | null>(null);
  const [unreachable, setUnreachable] = useState(false);

  useEffect(() => {
    let cancelled = false;
    async function poll() {
      try {
        const latest = await getPipelineStatus(novelId);
        if (cancelled) return;
        setStatus(latest);
        setUnreachable(false);
      } catch {
        if (!cancelled) setUnreachable(true);
      }
    }
    poll();
    const timer = setInterval(poll, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [novelId]);

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
