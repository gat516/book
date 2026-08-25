import { readerId } from "./readerId";
import type {
  AskResponse,
  ChapterResponse,
  CreateNovelRequest,
  CreateNovelResponse,
  EntityResponse,
  NovelListResponse,
  NovelSummary,
  PasteChapterRequest,
  PasteChapterResponse,
  Progress,
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

export function pasteChapter(novelId: string, body: PasteChapterRequest): Promise<PasteChapterResponse> {
  return request(`/novels/${novelId}/chapters`, { method: "POST", body: JSON.stringify(body) });
}

export function getChapter(novelId: string, n: number): Promise<ChapterResponse> {
  return request(`/novels/${novelId}/chapter/${n}`);
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

export function putProgress(novelId: string, chapter: number): Promise<Progress> {
  return request(`/novels/${novelId}/progress`, {
    method: "PUT",
    body: JSON.stringify({ chapter }),
  });
}

export { ApiError };
