import assert from "node:assert/strict";
import test from "node:test";
import { chapterKnowledgeReviewLabel, graphCoverageLabel } from "../src/knowledgeLabels.ts";

test("reader-feature coverage explains whether work is current, active, or not started", () => {
  const base = {
    novel_id: "n", active_generation_id: "g", active_state: "published",
    predecessor_generation_id: null, has_predecessor: false,
    eligible_chapters: 12, published_chapters: 12, missing_chapters: 0, discardable: false,
    running: false,
  } as const;
  assert.equal(graphCoverageLabel(base), "Up to date · 12 of 12 chapters ready");
  assert.equal(graphCoverageLabel({ ...base, running: true, published_chapters: 4, missing_chapters: 8 }), "Building · 4 of 12 chapters ready");
  assert.equal(graphCoverageLabel({ ...base, running: false, published_chapters: 4, missing_chapters: 8 }), "Paused · 4 of 12 chapters ready");
  assert.equal(graphCoverageLabel({ ...base, has_predecessor: true, predecessor_generation_id: "old", active_state: "active", running: true, published_chapters: 4, missing_chapters: 8 }), "Refreshing · 4 of 12 chapters ready");
  assert.equal(graphCoverageLabel({ ...base, has_predecessor: true, predecessor_generation_id: "old", active_state: "active", published_chapters: 4, missing_chapters: 8 }), "Refresh paused · 4 of 12 chapters ready");
  assert.equal(graphCoverageLabel({ ...base, active_generation_id: null }), "Not started");
});

test("chapter review action identifies the knowledge source chapter", () => {
  assert.equal(chapterKnowledgeReviewLabel(37), "Advanced: inspect story details from chapter 37");
});
