import assert from "node:assert/strict";
import { test } from "node:test";
import { applyRenderingChoices, lastMentionPerEntity, segment } from "../src/readerSegments.ts";

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

test("only the last mention of each entity survives", () => {
  const text = "Ling Feng struck. Ling Feng turned to the Hall.";
  const kept = lastMentionPerEntity(text, [
    { char_start: 0, char_end: 9, entity_id: "lf" },
    { char_start: 18, char_end: 27, entity_id: "lf" },
    { char_start: 41, char_end: 45, entity_id: "hall" },
  ]);
  assert.deepEqual(kept.map(s => [s.char_start, s.entity_id]), [[18, "lf"], [41, "hall"]]);
});

test("unlinked mentions collapse by surface, so distinct names stay reachable", () => {
  const text = "Ann met Bo. Ann waved at Bo.";
  const kept = lastMentionPerEntity(text, [
    { char_start: 0, char_end: 3, entity_id: null },
    { char_start: 8, char_end: 10, entity_id: null },
    { char_start: 12, char_end: 15, entity_id: null },
    { char_start: 25, char_end: 27, entity_id: null },
  ]);
  // One anchor per distinct name, each at its final occurrence — not one per occurrence,
  // and not a single anchor swallowing both names.
  assert.deepEqual(kept.map(s => [s.char_start, text.slice(s.char_start, s.char_end)]),
    [[12, "Ann"], [25, "Bo"]]);
});

test("last mention is chosen by codepoint offset, not array order", () => {
  const kept = lastMentionPerEntity("😀 Ann met Ann.", [
    { char_start: 10, char_end: 13, entity_id: "ann" },
    { char_start: 2, char_end: 5, entity_id: "ann" },
  ]);
  assert.deepEqual(kept.map(s => s.char_start), [10]);
});

test("the surviving spans still segment cleanly", () => {
  const text = "Ann met Ann.";
  const spans = [
    { char_start: 0, char_end: 3, entity_id: "ann" },
    { char_start: 8, char_end: 11, entity_id: "ann" },
  ];
  const pieces = segment(text, lastMentionPerEntity(text, spans));
  assert.equal(pieces.map(p => p.text).join(""), text);
  assert.deepEqual(pieces.filter(p => p.mention).map(p => p.text), ["Ann"]);
});

test("rendering choices survive last-mention reduction and segmentation", () => {
  const rendering = {
    source_term: "契科夫",
    target_term: null,
    status: "pending" as const,
    term_role: "foreign_person" as const,
    candidates: [
      { target_term: "Chekhov", pronunciation: [], segmentation: "", method: "restored_name" as const },
      { target_term: "Chekov", pronunciation: [], segmentation: "", method: "restored_name" as const },
    ],
  };
  const text = "Chekov spoke. Chekov left.";
  const pieces = segment(text, lastMentionPerEntity(text, [
    { char_start: 0, char_end: 6, entity_id: null, rendering },
    { char_start: 14, char_end: 20, entity_id: null, rendering },
  ]));
  const mention = pieces.find(piece => piece.mention);
  assert.equal(mention?.text, "Chekov");
  assert.deepEqual(mention?.rendering, rendering);
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

test("pending and unlocked spellings leave saved prose untouched", () => {
  const text = "Chekov spoke.";
  for (const status of ["pending", "unlocked"] as const) {
    const rendered = applyRenderingChoices(text, [{
      char_start: 0, char_end: 6, entity_id: null,
      rendering: { source_term: "契科夫", target_term: "Chekhov", status,
        term_role: "foreign_person", candidates: [] },
    }]);
    assert.equal(rendered.text, text);
    assert.deepEqual(rendered.spans.map(span => [span.char_start, span.char_end]), [[0, 6]]);
  }
});
