import type { ChapterListItem } from "./types";

export type StatusTone = "quiet" | "live" | "warn" | "bad";
export interface ChapterStatusLabel { label: string; tone: StatusTone }

export function readingState(chapter: ChapterListItem, active: boolean): ChapterStatusLabel {
  if (chapter.status === "done") return { label: "Ready to read", tone: "live" };
  if (chapter.status === "error") return { label: "Needs attention", tone: "bad" };
  if (active) return { label: "Preparing", tone: "live" };
  if (chapter.status === "queued") return { label: "Waiting", tone: "warn" };
  if (chapter.status === "ingested") return { label: "Not started", tone: "quiet" };
  return { label: "Checking", tone: "quiet" };
}

export function readerFeaturesState(chapter: ChapterListItem, active: boolean): ChapterStatusLabel {
  if (chapter.graph_status === "done") return { label: "Ready", tone: "live" };
  if (chapter.graph_status === "error") return { label: "Needs attention", tone: "bad" };
  if (chapter.status !== "done") return { label: "After translation", tone: "quiet" };
  if (active) return { label: "Building", tone: "live" };
  return { label: "Waiting", tone: "warn" };
}
