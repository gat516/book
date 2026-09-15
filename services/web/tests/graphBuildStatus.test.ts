import assert from "node:assert/strict";
import { test } from "node:test";
import { graphStageState } from "../src/graphBuildStatus.ts";
import type { RepairTrack } from "../src/types.ts";
import { describeStage, stageProgress } from "../src/pipelineStages.ts";

test("a failed chapter in a rebuilding revision stops the stage timer", () => {
  const track = { state: "rebuilding", worker: {
    job_state: "failed", updated_at: "2026-09-10T01:13:02Z",
  }} as RepairTrack;
  const first = graphStageState(track, Date.parse("2026-09-10T01:14:00Z"));
  assert.equal(first.failed, true);
  assert.equal(first.stageEnd, Date.parse("2026-09-10T01:13:02Z"));
  assert.deepEqual(graphStageState(track, Date.parse("2026-09-10T01:15:00Z")), first);
});

test("a resumed chapter advances the timer and clears failed state", () => {
  const track = { state: "rebuilding", worker: {
    job_state: "processing", updated_at: "2026-09-10T01:13:02Z",
  }} as RepairTrack;
  assert.deepEqual(graphStageState(track, 100), {failed: false, stageEnd: 100});
});

test("processing status explains the reader outcome and groups internal substages", () => {
  assert.equal(describeStage("records"), "Finding facts and story events");
  assert.deepEqual(stageProgress("records"), { step: 5, total: 7 });
  assert.deepEqual(stageProgress("identity"), { step: 5, total: 7 });
  assert.equal(describeStage("future_stage"), "Working on reader features");
});
