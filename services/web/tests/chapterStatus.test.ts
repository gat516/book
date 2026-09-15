import assert from "node:assert/strict";
import test from "node:test";
import { readerFeaturesState, readingState } from "../src/chapterStatus.ts";
import type { ChapterListItem } from "../src/types.ts";

function chapter(status: string, graphStatus = "pending"): ChapterListItem {
  return {
    chapter_index: 1,
    part: 1,
    status,
    graph_status: graphStatus,
    translation_warning: null,
  };
}

test("reading and reader-feature progress remain separate", () => {
  assert.deepEqual(readingState(chapter("done"), true), { label: "Ready to read", tone: "live" });
  assert.deepEqual(readerFeaturesState(chapter("done"), true), { label: "Building", tone: "live" });
  assert.deepEqual(readerFeaturesState(chapter("queued"), true), { label: "After translation", tone: "quiet" });
});

test("a post-translation failure does not make readable text look failed", () => {
  const item = chapter("done", "error");
  assert.deepEqual(readingState(item, false), { label: "Ready to read", tone: "live" });
  assert.deepEqual(readerFeaturesState(item, false), { label: "Needs attention", tone: "bad" });
});
