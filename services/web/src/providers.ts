import type { ProviderName } from "./types";

// Shared by the create form and the per-novel settings panel so the two cannot drift.
// Sensible per-provider defaults, so choosing a provider doesn't also require knowing its
// model names. Empty means "let the server decide".
export const DEFAULT_MODEL: Record<string, string> = {
  deepseek: "deepseek-chat",
  anthropic: "claude-haiku-4-5",
  gemini: "gemini-3.5-flash",
  ollama: "",
};

export const PROVIDER_LABELS: Record<ProviderName, string> = {
  anthropic: "Anthropic",
  deepseek: "DeepSeek",
  gemini: "Gemini",
  ollama: "Ollama (local)",
};

// Providers that authenticate with a key. Ollama is a local endpoint and takes none, so
// the form should not ask for one and "no key saved" is not a warning for it.
export const NEEDS_API_KEY: Record<ProviderName, boolean> = {
  anthropic: true,
  deepseek: true,
  gemini: true,
  ollama: false,
};
