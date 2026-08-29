import { readerId } from "./readerId";
import type {
  AskResponse,
  BootstrapGlossaryRequest,
  BootstrapGlossaryResponse,
  ChapterListResponse,
  CharacterNameReviewsResponse,
  ChapterResponse,
  CorrectGlossaryTermRequest,
  CorrectGlossaryTermResponse,
  CreateNovelRequest,
  CreateNovelResponse,
  EntityResponse,
  GlossaryResponse,
  NovelListResponse,
  NovelSummary,
  PipelineStatusResponse,
  TranslationHealth,
  PasteChapterRequest,
  PasteChapterResponse,
  Progress,
  ScrapeJobView,
  StartScrapeRequest,
} from "./types";

class ApiError extends Error {
  constructor(
    public status: number,
    // reader-api's error envelope is always {"error": "..."} (writeError in handlers.go).
    public code: string,
  ) {
    super(code);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...init,
    headers: {
      "X-Reader-ID": readerId(),
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...init?.headers,
    },
  });
  if (!response.ok) {
    const body = await response.text();
    let code = body || response.statusText;
    try {
      code = JSON.parse(body).error ?? code;
    } catch {
      // non-JSON error body: fall back to the raw text set above
    }
    throw new ApiError(response.status, code);
  }
  return response.json() as Promise<T>;
}

// Ungated on the server (novel metadata has no source_chapter to gate on) — the
// X-Reader-ID header sent by `request()` is simply ignored by reader-api for these.
export function listNovels(): Promise<NovelListResponse> {
  return request(`/novels`);
}

export function getNovel(novelId: string): Promise<NovelSummary> {
  return request(`/novels/${novelId}`);
}

export function createNovel(body: CreateNovelRequest): Promise<CreateNovelResponse> {
  return request(`/novels`, { method: "POST", body: JSON.stringify(body) });
}

// Irreversible: ingest-api cascades this to the novel's chapters, graph, glossary and
// queued work (migration 0030). The caller is responsible for confirming with the user.
export function deleteNovel(novelId: string): Promise<{ deleted: boolean }> {
  return request(`/novels/${novelId}`, { method: "DELETE" });
}

export function pasteChapter(novelId: string, body: PasteChapterRequest): Promise<PasteChapterResponse> {
  return request(`/novels/${novelId}/chapters`, { method: "POST", body: JSON.stringify(body) });
}

export function getChapter(novelId: string, n: number): Promise<ChapterResponse> {
  return request(`/novels/${novelId}/chapter/${n}`);
}

export function listChapters(novelId: string, limit: number, offset: number): Promise<ChapterListResponse> {
  return request(`/novels/${novelId}/chapters?limit=${limit}&offset=${offset}`);
}

// Where this reader left off. 404s for a reader who has never opened this novel — callers
// treat that as "start at the beginning" rather than an error.
export function getProgress(novelId: string): Promise<Progress> {
  return request(`/novels/${novelId}/progress`);
}

export function getPipelineStatus(novelId: string): Promise<PipelineStatusResponse> {
  return request(`/novels/${novelId}/pipeline`);
}

export function getEntity(novelId: string, entityId: string, at: number): Promise<EntityResponse> {
  return request(`/novels/${novelId}/entity/${entityId}?at=${at}`);
}

export function ask(novelId: string, question: string, at: number): Promise<AskResponse> {
  return request(`/novels/${novelId}/ask`, {
    method: "POST",
    body: JSON.stringify({ question, at }),
  });
}

export function startScrape(novelId: string, body: StartScrapeRequest): Promise<{ id: number }> {
  return request(`/novels/${novelId}/scrape`, { method: "POST", body: JSON.stringify(body) });
}

export function getScrapeStatus(novelId: string): Promise<ScrapeJobView> {
  return request(`/novels/${novelId}/scrape/status`);
}

export function cancelScrape(novelId: string): Promise<{ status: string }> {
  return request(`/novels/${novelId}/scrape/cancel`, { method: "POST" });
}

export function getGlossary(novelId: string, at?: number): Promise<GlossaryResponse> {
  return request(`/novels/${novelId}/glossary${at === undefined ? "" : `?at=${at}`}`);
}

export function correctGlossaryTerm(
  novelId: string,
  sourceTerm: string,
  body: CorrectGlossaryTermRequest,
): Promise<CorrectGlossaryTermResponse> {
  return request(`/novels/${novelId}/glossary/${encodeURIComponent(sourceTerm)}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export function bootstrapGlossary(
  novelId: string,
  body: BootstrapGlossaryRequest,
): Promise<BootstrapGlossaryResponse> {
  return request(`/novels/${novelId}/glossary/bootstrap`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function getCharacterNameReviews(novelId: string, chapter?: number): Promise<CharacterNameReviewsResponse> {
  const query = new URLSearchParams({ status: "pending" });
  if (chapter !== undefined) query.set("chapter", String(chapter));
  return request(`/novels/${novelId}/name-reviews?${query}`);
}

export function approveCharacterName(novelId: string, sourceTerm: string, targetTerm: string): Promise<unknown> {
  return request(`/novels/${novelId}/name-reviews/${encodeURIComponent(sourceTerm)}/approve`, {
    method: "POST",
    body: JSON.stringify({ target_term: targetTerm }),
  });
}

// Queue a window of chapters for translation starting at `from`. Omitting `count` lets the
// server apply the novel's own translate_lookahead. Safe to call repeatedly: the server
// only queues chapters not already queued, so a repeat returns an empty list.
export function translateAhead(
  novelId: string,
  from: number,
  count?: number,
): Promise<{ novel_id: string; queued: number[] }> {
  return request(`/novels/${novelId}/translate-ahead`, {
    method: "POST",
    body: JSON.stringify(count === undefined ? { from } : { from, count }),
  });
}

// A chapter's translation as it is being produced. `available` is false whenever nothing
// is streaming — before TRANSLATE starts, once the chapter is finished, or on a provider
// that can't stream — so callers render on `available`, not on truthiness of `text`.
export function getChapterPreview(
  novelId: string,
  n: number,
): Promise<{
  novel_id: string;
  chapter_index: number;
  available: boolean;
  text?: string;
  // Pipeline status of the chapter itself, so one read answers both "how far along is it"
  // and "is it readable now".
  status: string;
}> {
  return request(`/novels/${novelId}/chapter/${n}/preview`);
}

export function getTranslationHealth(novelId: string): Promise<TranslationHealth> {
  return request(`/novels/${novelId}/translation-health`);
}

export function putProgress(novelId: string, chapter: number): Promise<Progress> {
  return request(`/novels/${novelId}/progress`, {
    method: "PUT",
    body: JSON.stringify({ chapter }),
  });
}

export { ApiError };

export function deleteGlossaryTerm(novelId: string, sourceTerm: string, at: number): Promise<{ deleted: boolean }> {
  return request(`/novels/${novelId}/glossary/${encodeURIComponent(sourceTerm)}`, {
    method: "DELETE", body: JSON.stringify({ at_chapter: at }),
  });
}

export function prioritizeChapter(novelId: string, chapter: number): Promise<{ prioritized: boolean }> {
  return request(`/novels/${novelId}/translate-ahead`, {
    method: "POST", body: JSON.stringify({ from: chapter, count: 1, priority: true }),
  });
}

export function getKnowledgeStatus(novelId: string, chapter: number): Promise<import('./types').KnowledgeStatus> {
  return request(`/novels/${novelId}/knowledge-status?chapter=${chapter}`);
}
export function getRelationships(novelId: string, entityId: string, at: number): Promise<{relationships: import('./types').Relationship[]}> {
  return request(`/novels/${novelId}/relationships/${entityId}?at=${at}`);
}
