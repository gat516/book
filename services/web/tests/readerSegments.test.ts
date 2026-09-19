import assert from "node:assert/strict";
import { test } from "node:test";
import { applyRenderingChoices, segment } from "../src/readerSegments.ts";

test("unlinked names remain mentions, including codepoint offsets after emoji", () => {
  const pieces = segment("😀 Ling Feng met Ann.", [
    { char_start: 2, char_end: 11, entity_id: null },
    { char_start: 16, char_end: 19, entity_id: "known" },
  ]);
  assert.deepEqual(pieces.filter(p => p.mention), [
    { text: "Ling Feng", mention: true, entityId: null },
    { text: "Ann", mention: true, entityId: "known" },
  ]);
  assert.equal(pieces.map(p => p.text).join(""), "😀 Ling Feng met Ann.");
});

test("invalid and overlapping offsets do not duplicate or lose prose", () => {
  const pieces = segment("Ann left.", [
    { char_start: -1, char_end: 1, entity_id: null },
    { char_start: 0, char_end: 3, entity_id: "known" },
    { char_start: 1, char_end: 3, entity_id: null },
    { char_start: 4, char_end: 99, entity_id: null },
  ]);
  assert.equal(pieces.map(p => p.text).join(""), "Ann left.");
  assert.equal(pieces.filter(p => p.mention).length, 1);
});

test("every mention of a name stays interactive and carries its rendering", () => {
  const rendering = {
    source_term: "契科夫", target_term: null, status: "pending" as const,
    term_role: "foreign_person" as const, candidates: [],
  };
  const text = "Chekov spoke. Chekov left.";
  const pieces = segment(text, [
    { char_start: 0, char_end: 6, entity_id: null, rendering },
    { char_start: 14, char_end: 20, entity_id: null, rendering },
  ]);
  assert.equal(pieces.map(p => p.text).join(""), text);
  assert.deepEqual(pieces.filter(p => p.mention).map(p => p.text), ["Chekov", "Chekov"]);
  assert.ok(pieces.filter(p => p.mention).every(p => p.rendering === rendering));
});

test("confirmed spellings overlay every occurrence and adjust later offsets", () => {
  const rendering = {
    source_term: "碎星滩",
    target_term: "Shattered Star Beach",
    status: "locked" as const,
    term_role: "semantic_term" as const,
    candidates: [],
  };
  const original = "Wasted Star Trough met Yan at Wasted Star Trough.";
  const rendered = applyRenderingChoices(original, [
    { char_start: 0, char_end: 18, entity_id: null, rendering },
    { char_start: 23, char_end: 26, entity_id: "yan" },
    { char_start: 30, char_end: 48, entity_id: null, rendering },
  ]);
  assert.equal(rendered.text, "Shattered Star Beach met Yan at Shattered Star Beach.");
  assert.deepEqual(rendered.spans.map(span => [span.char_start, span.char_end]),
    [[0, 20], [25, 28], [32, 52]]);
  assert.equal(segment(rendered.text, rendered.spans).map(piece => piece.text).join(""), rendered.text);
});

test("only a confirmed spelling is painted over the text; a pending guess is not", () => {
  const text = "Ares spoke.";
  for (const status of ["pending", "unlocked", "locked"] as const) {
    const rendered = applyRenderingChoices(text, [{
      char_start: 0, char_end: 4, entity_id: null,
      rendering: { source_term: "阿瑞斯", target_term: "Aruisi", status,
        term_role: "foreign_person", candidates: [] },
    }]);
    assert.equal(rendered.text, status === "locked" ? "Aruisi spoke." : text);
    assert.equal(rendered.spans[0].rendering?.status, status);
  }
});
