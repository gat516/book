import assert from "node:assert/strict";
import test from "node:test";
import { buildWikiPage, otherIs } from "../src/wikiPage.ts";

const names = { a: "Abaddon", m: "Metatron", l: "Ling Feng", s: "Si You", y: "Yan" };
const fact = (category: string, subjects: string[], kind: string | null = null, chapter = 1) =>
  ({ chapter, category, kind, text: `${category} ${subjects.join(",")}`, subjects, version: "v", ordinal: 0 });

test("a relationship reads from either side", () => {
  const subordinate = fact("relationship", ["a", "m"], "subordinate");  // Abaddon is subordinate to Metatron
  assert.deepEqual(otherIs(subordinate, "m"), { other: "a", kind: "subordinate" });
  assert.deepEqual(otherIs(subordinate, "a"), { other: "m", kind: "superior" });
  const mentor = fact("relationship", ["s", "y"], "mentor");  // Si You is mentor to Yan
  assert.deepEqual(otherIs(mentor, "y"), { other: "s", kind: "mentor" });
  assert.equal(otherIs(mentor, "s"), null, "no known inverse, so not read from Si You's side");
});

test("a page groups facts into sections, six relationship headings, and More", () => {
  const page = buildWikiPage("a", [
    fact("intro", ["a"]),
    fact("relationship", ["a", "m"], "subordinate"),
    fact("relationship", ["a", "l"], "enemy"),
    fact("relationship", ["a", "y"], "mentor"),
    fact("ability", ["a"]), fact("event", ["a"], null, 2), fact("place", ["a"], null, 3),
  ], names);
  assert.equal(page.intro.length, 1);
  assert.deepEqual(page.relationships.map((g) => [g.heading, g.people.map((p) => p.name)]),
    [["Enemies", ["Ling Feng"]], ["Superiors", ["Metatron"]]]);
  assert.deepEqual(page.sections.map((s) => s.heading), ["Abilities"]);
  assert.deepEqual(page.history.map((e) => e.chapter), [2, 3]);
  assert.equal(page.more.length, 1, "mentor from the mentor's own side goes to More");
});

test("a fact that only mentions a character is not filed as theirs", () => {
  const page = buildWikiPage("m", [
    fact("intro", ["h", "m", "a"]),   // about Long Hao; Metatron only mentioned
    fact("intro", ["m"]),              // about Metatron
    fact("ability", ["l", "m"]),       // about Ling Feng
    fact("event", ["f", "m"]),         // an event naming Metatron: his history too
  ], names);
  assert.equal(page.intro.length, 1);
  assert.equal(page.sections.length, 0);
  assert.equal(page.history.length, 1);
  assert.equal(page.mentions.length, 2);
});
