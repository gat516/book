import type { WikiFact } from "./types";

// A character page is assembled here from their tagged facts; nothing about it is
// written by a model. Relationship facts are stored as "<A> is <kind> to <B>" with the
// subjects in that order, so a page can say who the OTHER person is to this character.

// Which stored fact an entry came from, so a reader can retract it.
export interface FactRef { chapter: number; version: string; ordinal: number }
export interface Entry { text: string; chapter: number; ref: FactRef }
export interface Relation { name: string; subject: string; chapter: number; text: string; ref: FactRef }
export interface WikiPageModel {
  intro: Entry[];
  aliases: Entry[];
  relationships: { heading: string; people: Relation[] }[];
  sections: { heading: string; entries: Entry[] }[];
  history: Entry[];
  more: Entry[];
  mentions: Entry[];
}

// The relationship headings a page shows, in order. Any other kind the model names is
// kept on the "More" tab with its fact.
const HEADINGS: [string, string][] = [
  ["family", "Family"], ["lover", "Lovers"], ["friend", "Friends"],
  ["enemy", "Enemies"], ["superior", "Superiors"], ["subordinate", "Subordinates"],
];
const FAMILY = new Set(["family", "parent", "child", "sibling", "mother", "father", "son", "daughter",
  "brother", "sister", "grandfather", "grandmother", "grandson", "granddaughter"]);
// What the other person is to this character, when the fact is phrased from this
// character's side ("<this> is subordinate to <other>" makes <other> a superior).
const INVERSE: Record<string, string> = { subordinate: "superior", superior: "subordinate" };
const SYMMETRIC = new Set(["friend", "enemy", "lover", "family", "sibling", "brother", "sister"]);

const SECTIONS: [string, string][] = [
  ["ability", "Abilities"], ["item", "Possessions"], ["affiliation", "Affiliations"], ["status", "Status"],
];

function heading(kind: string): string | null {
  const key = FAMILY.has(kind) ? "family" : kind;
  return HEADINGS.find(([k]) => k === key)?.[1] ?? null;
}

/** The other person's kind relative to `subject`, or null when it can't be read that way. */
export function otherIs(fact: WikiFact, subject: string): { other: string; kind: string } | null {
  const [first, second] = fact.subjects;
  if (!fact.kind || !first || !second || first === second) return null;
  if (second === subject) return { other: first, kind: fact.kind };
  if (first === subject) {
    if (INVERSE[fact.kind]) return { other: second, kind: INVERSE[fact.kind] };
    if (SYMMETRIC.has(fact.kind)) return { other: second, kind: fact.kind };
  }
  return null;
}

export function buildWikiPage(subject: string, facts: WikiFact[], names: Record<string, string>): WikiPageModel {
  const page: WikiPageModel = { intro: [], aliases: [], relationships: [], sections: [], history: [], more: [], mentions: [] };
  const byHeading = new Map<string, Relation[]>();
  const byCategory = new Map<string, Entry[]>();
  for (const fact of facts) {
    const ref = { chapter: fact.chapter, version: fact.version, ordinal: fact.ordinal };
    const entry = { text: fact.text, chapter: fact.chapter, ref };
    // A fact is about its first named character ("<A> holds ...", "<A> is <kind> to <B>").
    // Events and places belong to everyone they name; any other fact that only mentions
    // this character is kept apart, not filed as their intro or ability.
    const about = fact.subjects[0] === subject;
    if (fact.category === "event" || fact.category === "place") page.history.push(entry);
    else if (fact.category !== "relationship" && !about) page.mentions.push(entry);
    else if (fact.category === "intro") page.intro.push(entry);
    else if (fact.category === "alias") page.aliases.push(entry);
    else if (fact.category === "relationship") {
      const read = otherIs(fact, subject);
      const label = read && heading(read.kind);
      if (read && label) {
        const people = byHeading.get(label) ?? [];
        people.push({ name: names[read.other] ?? "Someone", subject: read.other, chapter: fact.chapter, text: fact.text, ref });
        byHeading.set(label, people);
      } else page.more.push(entry);
    } else (byCategory.get(fact.category) ?? byCategory.set(fact.category, []).get(fact.category)!).push(entry);
  }
  page.relationships = HEADINGS.map(([, label]) => ({ heading: label, people: byHeading.get(label) ?? [] }))
    .filter((group) => group.people.length);
  page.sections = SECTIONS.map(([category, label]) => ({ heading: label, entries: byCategory.get(category) ?? [] }))
    .filter((section) => section.entries.length);
  return page;
}
