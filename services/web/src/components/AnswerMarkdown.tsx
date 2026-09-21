import { memo } from "react";
import Markdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Parent, Root, RootContent } from "mdast";
import type { RetrievedSource } from "../types";

interface Props {
  answer: string;
  sources: RetrievedSource[];
  at: number;
}

// §0.3/§8: citation decoration uses the authorized retrieval response. Generated
// source labels never initiate a fetch or grant access to a different chapter.
function remarkCitations({ sources, at }: Pick<Props, "sources" | "at">) {
  const known = new Set(sources.filter(source => source.chapter <= at)
    .map(source => `${source.kind}:${source.id}:${source.chapter}`));
  function split(text: string): RootContent[] {
    const result: RootContent[] = [];
    let cursor = 0;
    let previousChapter: number | null = null;
    for (const match of text.matchAll(/\[chunk:([^\]\s]+)\s+ch:(\d+)\]/g)) {
      const between = text.slice(cursor, match.index);
      if (between) result.push({ type: "text", value: between });
      if (between.trim()) previousChapter = null;
      const chapter = Number(match[2]);
      const matched = known.has(`chunk:${match[1]}:${chapter}`);
      if (!matched || previousChapter !== chapter) result.push({ type: "link", url: "#answer-source", children: [{ type: "text", value: matched ? `Ch. ${chapter}` : "Source unavailable" }],
        data: { hProperties: { "data-answer-citation": matched ? "source" : "unavailable", title: matched ? `Source: chapter ${chapter}` : "This citation was not included in the retrieved sources." } } });
      previousChapter = matched ? chapter : null;
      cursor = match.index + match[0].length;
    }
    if (cursor < text.length) result.push({ type: "text", value: text.slice(cursor) });
    return result;
  }
  function walk(parent: Parent) {
    parent.children = parent.children.flatMap(node => {
      if (node.type === "text") return split(node.value);
      // Preserve code literals and avoid manufacturing nested links.
      if ("children" in node && node.type !== "link" && node.type !== "linkReference") walk(node);
      return [node];
    });
  }
  return (tree: Root) => walk(tree);
}

const components: Components = {
  h1: ({ children }) => <h3>{children}</h3>,
  h2: ({ children }) => <h3>{children}</h3>,
  a: ({ children, node, title }) => {
    const citation = node?.properties["data-answer-citation"];
    return <span className={citation ? `ask-citation${citation === "unavailable" ? " ask-citation-unavailable" : ""}` : undefined} title={title}>{children}</span>;
  },
  // Answers are untrusted generated content, never a source of remote resources.
  img: () => null,
  table: ({ children }) => <div className="ask-answer-table" role="region" aria-label="Answer table" tabIndex={0}><table>{children}</table></div>,
};

export const AnswerMarkdown = memo(function AnswerMarkdown({ answer, sources, at }: Props) {
  return <div className="ask-answer-markdown"><Markdown skipHtml components={components}
    remarkPlugins={[remarkGfm, [remarkCitations, { sources, at }]]}>{answer}</Markdown></div>;
});
