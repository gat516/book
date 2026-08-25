import { readerId } from "./readerId";
import type { AskResponse, ChapterResponse, EntityResponse, Progress } from "./types";

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
