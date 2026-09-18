import assert from "node:assert/strict";
import test from "node:test";
import { graphCoverageLabel } from "../src/knowledgeLabels.ts";

test("book progress says whether every readable chapter has its names and facts", () => {
  const base = {
    novel_id: "n", active_generation_id: "g", active_state: "published",
    predecessor_generation_id: null, has_predecessor: false,
    eligible_chapters: 12, published_chapters: 12, missing_chapters: 0, discardable: false,
    running: false,
  } as const;
  assert.equal(graphCoverageLabel(base), "Up to date · 12 of 12 chapters ready");
  assert.equal(graphCoverageLabel({ ...base, running: true, published_chapters: 4, missing_chapters: 8 }), "Working · 4 of 12 chapters ready");
  assert.equal(graphCoverageLabel({ ...base, published_chapters: 4, missing_chapters: 8 }), "4 of 12 chapters ready");
  assert.equal(graphCoverageLabel({ ...base, eligible_chapters: 0, published_chapters: 0 }), "No readable chapters yet");
});
