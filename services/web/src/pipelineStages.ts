// Names emitted by pipeline.stages.DEFAULT_STAGES, in execution order. Keep `publish`
// as a tolerated terminal slot for workers that split publication out of RECORDS.
export const PIPELINE_STAGES = ["chunk", "translate", "character_names", "scan", "records", "display_scan", "publish"] as const;

const STAGE_LABELS: Record<string, string> = {
  chunk: "Splitting into chunks",
  translate: "Translating",
  character_names: "Checking character names",
  scan: "Finding named mentions",
  records: "Extracting records",
  display_scan: "Aligning translated mentions",
  publish: "Publishing records",
  discover: "Discovering records",
  parse: "Parsing records",
  checks: "Checking record structure",
  identity: "Resolving identities",
  render: "Rendering records in English",
};

export function describeStage(stage?: string): string {
  if (!stage) return "Starting…";
  return STAGE_LABELS[stage] ?? `Pipeline stage: ${stage}`;
}

export function stageProgress(stage?: string): { step: number; total: number } {
  const index = PIPELINE_STAGES.indexOf(stage as typeof PIPELINE_STAGES[number]);
  return { step: index >= 0 ? index + 1 : 1, total: PIPELINE_STAGES.length };
}
