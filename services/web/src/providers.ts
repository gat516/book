import type { ProviderConfigView, ProviderName } from "./types";

// Shared by the create form and the per-novel settings panel so the two cannot drift.

export interface ModelOption {
  id: string;
  label: string;
  // One line on why you'd pick it. This pipeline runs many sequential calls per chapter
  // (one per CHARACTER_NAMES batch, one per unresolved RESOLVE surface), so cost and
  // latency per call matter more here than raw capability on a single hard prompt.
  note?: string;
}

// Sentinel for the "type your own" row. Not a model id -- the form swaps in a text box.
export const CUSTOM_MODEL = "__custom__";

// Verified against each provider's own documentation, 2026-08-31. Model lineups move
// fast: DeepSeek retired deepseek-chat/deepseek-reasoner on 2026-07-24, so anything
// pinned to those names is already broken.
export const MODEL_OPTIONS: Record<ProviderName, ModelOption[]> = {
  gemini: [
    { id: "gemini-3.7-flash", label: "Gemini 3.7 Flash", note: "Latest stable; strongest for multi-step and structured output" },
    { id: "gemini-3.6-flash", label: "Gemini 3.6 Flash", note: "Previous generation, still stable" },
    { id: "gemini-3.5-flash", label: "Gemini 3.5 Flash", note: "Verified working against this pipeline's JSON stages" },
    { id: "gemini-3.5-flash-lite", label: "Gemini 3.5 Flash-Lite", note: "Cheapest; good for high-volume RESOLVE calls" },
    { id: "gemini-3.1-flash-lite", label: "Gemini 3.1 Flash-Lite", note: "Cost-efficient alternative" },
    { id: "gemini-2.5-pro", label: "Gemini 2.5 Pro", note: "Deep reasoning; slower and pricier per call" },
  ],
  deepseek: [
    { id: "deepseek-v4-flash", label: "DeepSeek V4 Flash", note: "Fast and cheap; replaces the retired deepseek-chat" },
    { id: "deepseek-v4-pro", label: "DeepSeek V4 Pro", note: "1M context, more capable, higher cost" },
  ],
  anthropic: [
    { id: "claude-haiku-4-5", label: "Claude Haiku 4.5", note: "Cheapest; sensible default for extraction" },
    { id: "claude-sonnet-5", label: "Claude Sonnet 5", note: "Balanced capability and cost" },
    { id: "claude-opus-5", label: "Claude Opus 5", note: "Most capable; best for difficult translation" },
  ],
  ollama: [
    { id: "qwen2.5:7b-instruct", label: "qwen2.5:7b-instruct", note: "Produces valid JSON here, but needs ~6GB RAM" },
    { id: "qwen3:4b-instruct-2507-q8_0", label: "Qwen3 4B Instruct (Q8)", note: "Fast structured extraction; facts are evidence-gated." },
    { id: "llama3.2:3b", label: "llama3.2:3b", note: "Fits easily; fails this pipeline's schema validation" },
  ],
};

// First entry of each list. Empty for none means "let the server decide".
export const DEFAULT_MODEL: Record<string, string> = {
  gemini: MODEL_OPTIONS.gemini[0].id,
  deepseek: MODEL_OPTIONS.deepseek[0].id,
  anthropic: MODEL_OPTIONS.anthropic[0].id,
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

// Ollama serves whatever happens to be pulled on the host, so its list above is only a
// hint -- a model the machine doesn't have is a runtime failure, not a validation one.
export const MODEL_LIST_IS_ADVISORY: Record<ProviderName, boolean> = {
  anthropic: false,
  deepseek: false,
  gemini: false,
  ollama: true,
};

// The entity graph structurally requires a loopback Ollama (KnowledgeEngine refuses
// anything else), so only an Ollama provider config's own extraction model is ever a
// sane default for a graph rebuild -- a book configured for Gemini has no matching entry
// in the graph track's (locally-installed) model list. Shared by RepairPanel (fresh
// rebuilds) and ChapterKnowledgeWorkspace (the one-time managed-graph build) so there is
// exactly one copy of this default rather than two that can drift.
export function defaultGraphExtractModel(config: ProviderConfigView | null): string {
  return config?.provider === "ollama" ? config.extract_model ?? "" : "";
}
