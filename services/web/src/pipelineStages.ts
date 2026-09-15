// Names emitted by pipeline.stages.DEFAULT_STAGES, in execution order. Keep `publish`
// as a tolerated terminal slot for workers that split publication out of RECORDS.
export const PIPELINE_STAGES = ["chunk", "translate", "character_names", "scan", "records", "display_scan", "publish"] as const;

const STAGE_LABELS: Record<string, string> = {
  chunk: "Preparing chapter text",
  translate: "Translating the chapter",
  character_names: "Checking name translations",
  scan: "Finding characters and places",
  records: "Finding facts and story events",
  display_scan: "Linking names in the translated chapter",
  publish: "Finishing reader features",
  discover: "Finding supported story details",
  parse: "Organizing story details",
  checks: "Checking details against the chapter",
  identity: "Linking names to characters",
  render: "Preparing story details for display",
};

const RECORD_SUBSTAGES = new Set(["discover", "parse", "checks", "identity", "render"]);

export function describeStage(stage?: string): string {
  if (!stage) return "Starting…";
  return STAGE_LABELS[stage] ?? "Working on reader features";
}

export function stageProgress(stage?: string): { step: number; total: number } {
  const visibleStage = stage && RECORD_SUBSTAGES.has(stage) ? "records" : stage;
  const index = PIPELINE_STAGES.indexOf(visibleStage as typeof PIPELINE_STAGES[number]);
  return { step: index >= 0 ? index + 1 : 1, total: PIPELINE_STAGES.length };
}
