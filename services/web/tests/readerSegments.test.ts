import assert from "node:assert/strict";
import { test } from "node:test";
import { lastMentionPerEntity, segment } from "../src/readerSegments.ts";

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
