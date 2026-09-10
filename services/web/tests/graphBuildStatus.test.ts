import assert from "node:assert/strict";
import { test } from "node:test";
import { graphStageState } from "../src/graphBuildStatus.ts";
import type { RepairTrack } from "../src/types.ts";

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
