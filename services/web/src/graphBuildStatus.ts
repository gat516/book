import type { RepairTrack } from "./types.ts";

export function graphStageState(track: RepairTrack, now: number) {
  const worker = track.worker;
  const failed = track.state === "failed" || worker?.job_state === "failed";
  const stopped = worker?.job_state === "failed" || worker?.job_state === "done";
  return { failed, stageEnd: stopped && worker ? Date.parse(worker.updated_at) : now };
}
