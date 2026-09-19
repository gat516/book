import assert from "node:assert/strict";
import test from "node:test";
import { factsCoverageLabel } from "../src/knowledgeLabels.ts";

test("book progress says whether every readable chapter has its names and facts", () => {
  const base = { novel_id: "n", eligible_chapters: 12, done_chapters: 12, missing_chapters: 0, running: false } as const;
  assert.equal(factsCoverageLabel(base), "Up to date · 12 of 12 chapters ready");
  assert.equal(factsCoverageLabel({ ...base, running: true, done_chapters: 4, missing_chapters: 8 }), "Working · 4 of 12 chapters ready");
  assert.equal(factsCoverageLabel({ ...base, done_chapters: 4, missing_chapters: 8 }), "4 of 12 chapters ready");
  assert.equal(factsCoverageLabel({ ...base, eligible_chapters: 0, done_chapters: 0 }), "No readable chapters yet");
});
