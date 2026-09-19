import assert from "node:assert/strict";
import test from "node:test";
import { parseInline, parseWikiPage } from "../src/wikiMarkdown.ts";

test("a page becomes title, aliases, sections, paragraphs and lists", () => {
  const blocks = parseWikiPage([
    "# Hero",
    "Also known as: Masked One (ch. 4)",
    "A wanderer from the north.",
    "",
    "## History",
    "He left home (ch. 1). He came back",
    "changed (ch. 3–4).",
    "",
    "## Relationships",
    "- **Mentor**: taught him (ch. 2)",
    "- **Rival**: fought him",
  ].join("\n"));
  assert.deepEqual(blocks.map((block) => block.kind), ["title", "aka", "paragraph", "heading", "paragraph", "heading", "list"]);
  const history = blocks[4];
  assert.ok(history.kind === "paragraph");
  assert.deepEqual(history.parts.filter((part) => part.kind === "cite").map((part) => part.kind === "cite" && part.chapters), ["1", "3–4"]);
  const list = blocks[6];
  assert.ok(list.kind === "list" && list.items.length === 2);
});

test("inline markup is structure only, never HTML", () => {
  assert.deepEqual(parseInline("<b>x</b> **y** (ch. 5, 6)"), [
    { kind: "text", text: "<b>x</b> " },
    { kind: "bold", text: "y" },
    { kind: "text", text: " " },
    { kind: "cite", chapters: "5, 6" },
  ]);
});
