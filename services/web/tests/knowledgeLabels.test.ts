import assert from "node:assert/strict";
import test from "node:test";
import { chapterKnowledgeReviewLabel, graphCoverageLabel } from "../src/knowledgeLabels.ts";

test("graph coverage distinguishes the current graph from an active replacement", () => {
  const base = {
    novel_id: "n", active_generation_id: "g", active_state: "published",
    predecessor_generation_id: null, has_predecessor: false,
    eligible_chapters: 12, published_chapters: 12, missing_chapters: 0, discardable: false,
  } as const;
  assert.equal(graphCoverageLabel(base), "Current graph coverage: 12/12 chapters published.");
  assert.match(graphCoverageLabel({ ...base, has_predecessor: true, predecessor_generation_id: "old", active_state: "active", published_chapters: 4, missing_chapters: 8 }), /Replacement graph is active: 4\/12 chapters published/);
  assert.equal(graphCoverageLabel({ ...base, active_generation_id: null }), "Knowledge graph has not been started for this book.");
});

test("chapter review action identifies the knowledge source chapter", () => {
  assert.equal(chapterKnowledgeReviewLabel(37), "Review knowledge added in chapter 37");
});
