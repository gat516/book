import assert from "node:assert/strict";
import test from "node:test";
import { uniqueChapterRenderings } from "../src/recordPresentation.ts";

test("chapter term summaries keep one row per source/target decision", () => {
  const rendering = (source_term: string, target_term: string | null) => ({
    source_term,
    target_term,
    status: "locked" as const,
    term_role: "chinese_person" as const,
    candidates: [],
  });
  const spans = [
    { char_start: 0, char_end: 2, entity_id: null, rendering: rendering("林", "Lin") },
    { char_start: 8, char_end: 10, entity_id: null, rendering: rendering("林", "Lin") },
    { char_start: 16, char_end: 18, entity_id: null, rendering: rendering("林", "Lynn") },
  ];
  assert.deepEqual(uniqueChapterRenderings(spans), [
    rendering("林", "Lin"),
    rendering("林", "Lynn"),
  ]);
});

test("spans without a rendering do not create a guessed term", () => {
  assert.deepEqual(uniqueChapterRenderings([{ char_start: 0, char_end: 2, entity_id: null }]), []);
});
