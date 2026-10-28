import { describe, expect, it } from "vitest";

import type { CiMerge } from "./agent-ci-merges";
import { ciRunTimeline } from "./agent-ci-timeline";
import type { AgentChangeProposal, AgentVersionWorkbench } from "./agents-api";

const BASE = {
  schema_version: 1,
  current_eval_policy_revision_id: null,
  live_identity: { release_id: "arl_1", release_sequence: 3, revision_id: "arv_live" },
  candidate_identity: null,
  next_draft_identity: null,
  active_plan_identity: null,
  active_snapshot_identity: null,
  active_override_identity: null,
  active_execution_summary: null,
  control_row_version: null,
  display_state: "draft",
  draft_rebase_state: "current",
  readiness: null,
  reason_codes: [],
  stale_subject: null,
  primary_action: { kind: "prepare_candidate", enabled: true, disabled_reason_codes: [] },
  secondary_actions: [],
} as AgentVersionWorkbench;

const CANDIDATE = { audience_mode: "private", candidate_hash: "h", candidate_id: "arc_2", revision_id: "arv_2", row_version: 1, state: "prepared" } as const;

const MERGE: CiMerge = {
  agentId: "osa_1",
  proposal: { proposal_id: "acp_git", source_type: "git" } as AgentChangeProposal,
  mergedAt: Date.parse("2026-10-18T08:00:00Z"),
  candidateIdAtMerge: null,
  preparedCandidateId: null,
};

const states = (steps: ReturnType<typeof ciRunTimeline>) => steps?.map((step) => `${step.key}:${step.state}`);

describe("ciRunTimeline", () => {
  it("is absent without a CI flow", () => {
    expect(ciRunTimeline(BASE, null)).toBeNull();
    expect(ciRunTimeline({ ...BASE, candidate_identity: CANDIDATE }, null)).toBeNull();
  });

  it("waits for CI to prepare after the Owner merges", () => {
    const steps = ciRunTimeline(BASE, MERGE);
    expect(states(steps)).toEqual(["merged:done", "prepared:current", "evaluating:waiting", "check:waiting"]);
    expect(steps![0]).toMatchObject({ at: "2026-10-18T08:00:00.000Z", actor: "owner" });
  });

  it("follows the comparison and ends at the release-check outcome", () => {
    const evaluating = ciRunTimeline({
      ...BASE,
      candidate_identity: CANDIDATE,
      active_execution_summary: { can_cancel: true, execution_id: "aex_1", failure_code: null, finished_at: null, row_version: 1, started_at: "2026-10-18T08:05:00Z", status: "running", progress: { cases: { completed: 3, total: 8 } } as never },
    }, MERGE);
    expect(states(evaluating)).toEqual(["merged:done", "prepared:done", "evaluating:current", "check:waiting"]);
    expect(evaluating![2].progress).toEqual({ completed: 3, total: 8 });

    const blocked = ciRunTimeline({
      ...BASE,
      candidate_identity: CANDIDATE,
      active_snapshot_identity: { id: "aevs_1", hash: "s", created_at: "2026-10-18T08:20:00Z", execution_id: "aex_1" },
      readiness: "blocked",
    }, { ...MERGE, preparedCandidateId: "arc_2" });
    expect(states(blocked)).toEqual(["merged:done", "prepared:done", "evaluating:done", "check:failed"]);
    expect(blocked![3]).toMatchObject({ outcome: "blocked", at: "2026-10-18T08:20:00Z" });
  });

  it("doesn't treat the candidate already on screen at merge time as CI's", () => {
    const merge = { ...MERGE, candidateIdAtMerge: "arc_2" };
    expect(states(ciRunTimeline({ ...BASE, candidate_identity: CANDIDATE }, merge))?.[1]).toBe("prepared:current");
  });

  it("recovers a CI candidate after a reload from the draft's CI changes, without a merge time", () => {
    const steps = ciRunTimeline({
      ...BASE,
      candidate_identity: CANDIDATE,
      draft_changes: [{ path: "/definition/system_prompt", live: "a", draft: "b", source: "ci" }],
    }, null);
    expect(states(steps)).toEqual(["merged:done", "prepared:done", "evaluating:waiting", "check:waiting"]);
    expect(steps![0]).toMatchObject({ at: null, actor: null });
  });
});
