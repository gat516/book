// The small subset of Markdown a wiki page uses, parsed into blocks the view renders as
// React elements. No HTML is ever injected: a page is model output, so it gets structure
// and nothing else.

export type Inline =
  | { kind: "text"; text: string }
  | { kind: "bold"; text: string }
  | { kind: "cite"; chapters: string };

export type Block =
  | { kind: "title"; text: string }
  | { kind: "heading"; text: string }
  | { kind: "aka"; parts: Inline[] }
  | { kind: "paragraph"; parts: Inline[] }
  | { kind: "list"; items: Inline[][] };

const INLINE = /\*\*(.+?)\*\*|\(ch\. ([\d–,\s]+)\)/g;

export function parseInline(text: string): Inline[] {
  const parts: Inline[] = [];
  let at = 0;
  for (const match of text.matchAll(INLINE)) {
    const start = match.index ?? 0;
    if (start > at) parts.push({ kind: "text", text: text.slice(at, start) });
    parts.push(match[1] !== undefined
      ? { kind: "bold", text: match[1] }
      : { kind: "cite", chapters: match[2].trim() });
    at = start + match[0].length;
  }
  if (at < text.length) parts.push({ kind: "text", text: text.slice(at) });
  return parts;
}

export function parseWikiPage(body: string): Block[] {
  const blocks: Block[] = [];
  let paragraph: string[] = [];
  let list: Inline[][] | null = null;
  const flush = () => {
    if (paragraph.length) blocks.push({ kind: "paragraph", parts: parseInline(paragraph.join(" ")) });
    if (list) blocks.push({ kind: "list", items: list });
    paragraph = [];
    list = null;
  };
  for (const raw of body.split("\n")) {
    const line = raw.trim();
    if (!line) { flush(); continue; }
    if (line.startsWith("## ")) { flush(); blocks.push({ kind: "heading", text: line.slice(3).trim() }); continue; }
    if (line.startsWith("# ")) { flush(); blocks.push({ kind: "title", text: line.slice(2).trim() }); continue; }
    if (/^also known as:/i.test(line)) {
      flush();
      blocks.push({ kind: "aka", parts: parseInline(line.replace(/^also known as:\s*/i, "")) });
      continue;
    }
    const bullet = line.match(/^[-*•]\s+(.*)$/);
    if (bullet) {
      if (paragraph.length) { blocks.push({ kind: "paragraph", parts: parseInline(paragraph.join(" ")) }); paragraph = []; }
      (list ??= []).push(parseInline(bullet[1]));
      continue;
    }
    if (list) { blocks.push({ kind: "list", items: list }); list = null; }
    paragraph.push(line);
  }
  flush();
  return blocks;
}
