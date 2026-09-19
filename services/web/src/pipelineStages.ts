// Names emitted by pipeline.stages.DEFAULT_STAGES, in execution order.
export const PIPELINE_STAGES = ["chunk", "translate", "display_scan", "facts", "chunk_index"] as const;

const STAGE_LABELS: Record<string, string> = {
  chunk: "Preparing chapter text",
  translate: "Translating the chapter",
  display_scan: "Linking names in the translated chapter",
  facts: "Finding facts for the wiki",
  chunk_index: "Indexing the chapter for questions",
};

export function describeStage(stage?: string): string {
  if (!stage) return "Starting…";
  return STAGE_LABELS[stage] ?? "Working on reader features";
}

export function stageProgress(stage?: string): { step: number; total: number } {
  const index = PIPELINE_STAGES.indexOf(stage as typeof PIPELINE_STAGES[number]);
  return { step: index >= 0 ? index + 1 : 1, total: PIPELINE_STAGES.length };
}
