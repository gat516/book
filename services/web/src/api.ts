import { failureExplanation } from "./factsStatus";
import { sessionHeaders, sessionExpired } from "./session";
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
  GlossaryResponse,
  NovelListResponse,
  NovelSummary,
  PipelineStatusResponse,
  TranslationHealth,
  PasteChapterRequest,
  ProviderConfigView,
  ProviderCredentialsResponse,
  SaveProviderCredentialRequest,
  SaveProviderConfigRequest,
  PasteChapterResponse,
  Progress,
  ScrapeJobView,
  StartScrapeRequest,
  BookFactsStatus,
  ChapterFactsStatusResponse,
} from "./types";

class ApiError extends Error {
  constructor(
    public status: number,
    // reader-api's error envelope is always {"error": "..."} (writeError in handlers.go).
    public code: string,
    public body?: unknown,
  ) {
    super(code);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      ...init,
      headers: {
        ...sessionHeaders(),
        ...(init?.body ? { "Content-Type": "application/json" } : {}),
        ...init?.headers,
      },
    });
  } catch {
    // The browser couldn't even reach reader-api (dev server down, network drop) — a raw
    // "Failed to fetch" TypeError is not a message a reader should have to interpret.
    throw new ApiError(0, "Could not reach the server. Check your connection and try again.");
  }
  if (response.status === 401) sessionExpired();
  if (!response.ok) {
    const body = await response.text();
    let code = body || response.statusText;
    let parsed: unknown;
    try {
      parsed = JSON.parse(body);
      if (parsed && typeof parsed === "object" && "error" in parsed) {
        const value = (parsed as { error?: unknown }).error;
        if (typeof value === "string") code = value;
      }
    } catch {
      // Not our JSON error envelope — most likely a missing route or a proxy/gateway
      // failure (e.g. Go's bare "404 page not found"), not something meant for a reader
      // to see verbatim. Keep the status code for anyone debugging; drop the raw text.
      code = `The server could not complete this request (${response.status}).`;
    }
    if (code === "ask-ai provider failure" && parsed && typeof parsed === "object" && "category" in parsed) {
      const category = (parsed as { category?: unknown }).category;
      code = failureExplanation({ retry_category: typeof category === "string" ? category : null });
    }
    throw new ApiError(response.status, code, parsed);
  }
  return response.json() as Promise<T>;
}

// Novel metadata is ungated, but the optional X-Reader-ID lets the list include this
// reader's saved current chapter without fetching progress once per book.
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

export function getChapterFactsStatus(novelId: string, chapter: number): Promise<ChapterFactsStatusResponse> {
  return request(`/novels/${novelId}/chapter/${chapter}/facts/status`);
}
export function getWikiPages(novelId: string): Promise<import("./types").WikiPagesResponse> {
  return request(`/novels/${novelId}/wiki/pages`);
}

export function getWikiPage(novelId: string, subject: string): Promise<import("./types").WikiPageResponse> {
  return request(`/novels/${novelId}/wiki/pages/${encodeURIComponent(subject)}`);
}

export function getWikiEvents(novelId: string): Promise<import("./types").WikiEventsResponse> {
  return request(`/novels/${novelId}/wiki/events`);
}

export function retractFact(novelId: string, fact: { chapter: number; version: string; ordinal: number }): Promise<{ retracted: boolean }> {
  return request(`/novels/${novelId}/wiki/facts/retract`, {
    method: "POST",
    body: JSON.stringify({ chapter: fact.chapter, version: fact.version, ordinal: fact.ordinal }),
  });
}

export function retryFacts(novelId: string, chapter: number): Promise<{ retried: boolean; chapter_index: number }> {
  return request(`/novels/${novelId}/chapter/${chapter}/facts/retry`, { method: "POST" });
}
// Finds missing facts: every readable chapter without them is queued in order; chapters
// that already have facts are left alone.
export function extractFacts(novelId: string): Promise<{ chapters_enqueued: number }> {
  return request(`/novels/${novelId}/facts/extract`, { method: "POST" });
}
// Pauses the whole book's facts work. Finished chapters keep their facts.
export function stopFacts(novelId: string): Promise<{ stopped: boolean; chapters_stopped: number }> {
  return request(`/novels/${novelId}/facts/stop`, { method: "POST" });
}
export function getFactsStatus(novelId: string): Promise<BookFactsStatus> {
  return request(`/novels/${novelId}/facts/status`);
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

export type QueueMode = "all" | "focused" | "paused";
export interface QueueControl {
  mode: QueueMode;
  focus_novel_id: string;
	mode_changed_at?: string;
	mode_changed_by?: string;
	mode_reason?: string;
  // Presence of the worker's Redis heartbeat (TTL-bounded, so true means it checked in
  // recently). Optional, and read strictly against `false`, so a backend predating the
  // field reads as "unknown" rather than reporting a healthy worker as offline.
  worker_alive?: boolean;
  books: Array<{
    novel_id: string;
    title: string;
    pending: number;
    in_flight: Array<{ chapter_index: number; stage: string }>;
  }>;
}
export function getQueueControl(): Promise<QueueControl> {
  return request("/queue");
}
// Serialize navigation writes in this tab so a slow response from book A cannot
// override the newer selection of book B. Other tabs share the same library policy.
let queueUpdates: Promise<unknown> = Promise.resolve();
export function updateQueueControl(patch: { mode?: QueueMode; focus_novel_id?: string; reason?: string }): Promise<QueueControl> {
  const update = queueUpdates.then(() => request<QueueControl>("/queue", {
    method: "PATCH", body: JSON.stringify(patch),
  }));
  queueUpdates = update.catch(() => undefined);
  return update;
}

export function ask(novelId: string, question: string, at: number): Promise<AskResponse> {
  return request(`/novels/${novelId}/ask`, {
    method: "POST",
    body: JSON.stringify({ question, at }),
  });
}

export function previewScrape(novelId: string, url: string): Promise<import("./types").ScrapePreview> {
  return request(`/novels/${novelId}/scrape/preview`, { method: "POST", body: JSON.stringify({ url }) });
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

export function confirmGlossaryTerm(
  novelId: string,
  body: { source_term: string; target_term: string; at_chapter: number; term_role: import("./types").TermRole },
): Promise<CorrectGlossaryTermResponse> {
  return request(`/novels/${novelId}/glossary/confirm`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function getCharacterNameReviews(novelId: string, chapter?: number): Promise<CharacterNameReviewsResponse> {
  const query = new URLSearchParams({ status: "pending" });
  if (chapter !== undefined) query.set("chapter", String(chapter));
  return request(`/novels/${novelId}/name-reviews?${query}`);
}

export function approveCharacterName(novelId: string, sourceTerm: string, targetTerm: string, termRole: import("./types").TermRole): Promise<unknown> {
  return request(`/novels/${novelId}/name-reviews/${encodeURIComponent(sourceTerm)}/approve`, {
    method: "POST",
    body: JSON.stringify({ target_term: targetTerm, term_role: termRole }),
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
  // Safe provider category derived by reader-api from the latest chapter failure.
  failure_category?: string;
}> {
  return request(`/novels/${novelId}/chapter/${n}/preview`);
}

export function getTranslationHealth(novelId: string): Promise<TranslationHealth> {
  return request(`/novels/${novelId}/translation-health`);
}

export async function getProviderHealth(
  novelId: string,
  track: import("./types").ProviderHealthTrack,
): Promise<import("./types").ProviderHealth> {
  try {
    return await request(`/novels/${novelId}/provider-health?track=${track}`);
  } catch (err) {
    // Health deliberately answers with a safe JSON body on non-2xx so the UI can render
    // the cause. Transport failures still throw and are shown as a probe-unavailable state.
    if (err instanceof ApiError && err.body && typeof err.body === "object" &&
      "category" in err.body && "provider" in err.body && "endpoint_kind" in err.body) {
      return err.body as import("./types").ProviderHealth;
    }
    throw err;
  }
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

// null (not a thrown 404) when the novel has no provider_config row: "inheriting the
// server default" is an ordinary state for a novel, not an error the caller must catch.
export async function getProviderConfig(novelId: string): Promise<ProviderConfigView | null> {
  try {
    return await request<ProviderConfigView>(`/novels/${novelId}/provider-config`);
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) return null;
    throw err;
  }
}

export function saveProviderConfig(
  novelId: string,
  body: SaveProviderConfigRequest,
): Promise<ProviderConfigView> {
  return request(`/novels/${novelId}/provider-config`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export async function listOllamaModels(novelId: string, target: "configured" | "graph" = "configured"): Promise<string[]> {
  const query = target === "graph" ? "?target=graph" : "";
  const response = await request<{ models: string[] }>(`/novels/${novelId}/provider-config/ollama-models${query}`);
  return response.models;
}


// Global provider credentials. Ungated like the other administration routes; reads are
// masked server-side, so no key ever reaches the browser.
export function listProviderCredentials(): Promise<ProviderCredentialsResponse> {
  return request(`/provider-credentials`);
}

export function saveProviderCredential(
  provider: string,
  body: SaveProviderCredentialRequest,
): Promise<ProviderCredentialsResponse> {
  return request(`/provider-credentials/${provider}`, {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

// The only way to clear a stored key: an omitted key on save means "unchanged".
export async function deleteProviderCredential(provider: string): Promise<void> {
  const response = await fetch(`/api/provider-credentials/${provider}`, {
    method: "DELETE",
    headers: sessionHeaders(),
  });
  if (!response.ok) throw new Error("Could not remove provider key.");
}

export function getEmbeddingConfig(): Promise<import("./types").EmbeddingConfig> {
  return request("/embedding-config");
}
export function saveEmbeddingConfig(body: import("./types").EmbeddingConfig): Promise<import("./types").EmbeddingConfig> {
  return request("/embedding-config", { method: "PUT", body: JSON.stringify(body) });
}
