// Mirrors services/reader-api/models.go's JSON shapes 1:1. Keep in sync by hand — this
// is a two-service monorepo, not a shared-schema one (PLAN.md 5.1 scope).

export interface NovelSummary {
  id: string;
  title: string;
  source_lang: string;
  target_lang: string;
  genre: string | null;
  created_at: string;
}

export interface NovelListResponse {
  novels: NovelSummary[];
}

export interface CreateNovelRequest {
  title: string;
  source_lang?: string;
  target_lang?: string;
  genre?: string;
}

// ingest-api's createNovelResp, proxied verbatim by reader-api's POST /novels — NOT a
// full NovelSummary (creation returns only the generated id).
export interface CreateNovelResponse {
  id: string;
}

// ingest-api's pasteChapterReq/pasteChapterResp, proxied verbatim by reader-api's
// POST /novels/{id}/chapters.
export interface PasteChapterRequest {
  chapter_index: number;
  raw_text: string;
  translated_text?: string;
  site_chapter_no?: string;
}

export interface PasteChapterResponse {
  novel_id: string;
  chapter_index: number;
  raw_hash: string;
  status: string;
}

// reader-api's scrapeRequest/ScrapeJobView (services/reader-api/{handlers,models}.go).
export interface StartScrapeRequest {
  start_url: string;
  mode?: "translate" | "bootstrap";
}

export interface ScrapeJobView {
  id: number;
  novel_id: string;
  start_url: string;
  mode: string;
  status: "pending" | "running" | "done" | "error" | "cancelled";
  chapters_fetched: number;
  last_error: string | null;
  cancel_requested: boolean;
  created_at: string;
  updated_at: string;
}

export interface SpanView {
  entity_id: string;
  char_start: number;
  char_end: number;
}

export interface ChapterResponse {
  novel_id: string;
  chapter_index: number;
  // The reader's STORED PROGRESS (not chapter_index) — see HoverCard.tsx for why this
  // exact value, captured once per chapter load, is what every hover/ask on this
  // chapter view must use as `at`.
  at: number;
  text: string;
  spans: SpanView[];
  has_next: boolean;
}

export interface FactView {
  attribute: string;
  value: string;
  valid_from_chapter: number;
  source_chapter: number;
  confidence: number;
}

export interface EntitySummary {
  id: string;
  canonical: string;
  kind: string;
  first_seen_chapter: number;
}

export interface EntityView extends EntitySummary {
  aliases: string[];
  facts: FactView[];
}

export interface EntityResponse {
  novel_id: string;
  at: number;
  entity: EntityView;
}

export interface RetrievedSource {
  kind: "chunk" | "fact" | "edge";
  id: number;
  chapter: number;
}

export interface AskResponse {
  answer: string;
  at: number;
  retrieved_sources: RetrievedSource[];
  served_by: { provider: string; model: string } | null;
}

export interface Progress {
  novel_id: string;
  reader_id: string;
  current_chapter: number;
  updated_at: string;
}
