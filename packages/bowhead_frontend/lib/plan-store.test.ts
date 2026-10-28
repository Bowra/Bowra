import { afterEach, describe, expect, it } from "vitest";

import {
  _resetAllPlansForTesting,
  acknowledgePlanEdit,
  applyPlanProposed,
  applyPlanSnapshot,
  applyPlanStatus,
  applyPlanStepBlocked,
  applyPlanStepDone,
  applyPlanStepFailed,
  applyPlanStepStarted,
  deleteStep,
  dismissPlan,
  editStepObjective,
  getPlanSnapshot,
  reorderSteps,
  toggleStepIncluded,
} from "./plan-store";

const PID = "plan_x";

const PROPOSED = {
  plan_id: PID,
  title: "对青松医疗集团做尽职调查",
  steps: [
    { id: "s1", agent: "compliance-analyst", objective: "法律", level: 0, depends_on: [], needs_corpus: true },
    { id: "s2", agent: "compute", objective: "数据", level: 0, depends_on: [], needs_corpus: false },
    { id: "s3", agent: "canvas-writer", objective: "汇总", level: 1, depends_on: ["s1", "s2"], needs_corpus: false },
  ],
};

afterEach(() => _resetAllPlansForTesting());

describe("Plan preview revision", () => {
  it("keeps later local edits when an earlier save advances the revision", () => {
    applyPlanProposed({ ...PROPOSED, current_rev: 1 });
    editStepObjective(PID, "s1", "second local edit");
    acknowledgePlanEdit(PID, 1, 2);
    expect(getPlanSnapshot(PID)?.currentRev).toBe(2);
    expect(getPlanSnapshot(PID)?.steps[0].objective).toBe("second local edit");
    acknowledgePlanEdit(PID, 1, 2);
    acknowledgePlanEdit(PID, 2, 4);
    expect(getPlanSnapshot(PID)?.currentRev).toBe(2);
  });

  it("does not replace saved steps with an older proposal or state response", () => {
    applyPlanProposed({ ...PROPOSED, current_rev: 1 });
    applyPlanSnapshot({
      plan_id: PID, plan: { current_rev: 3 },
      steps: [{ ...PROPOSED.steps[0], objective: "latest confirmed" }],
    });
    applyPlanSnapshot({ plan_id: PID, plan: { current_rev: 2 }, steps: PROPOSED.steps });
    applyPlanProposed({ ...PROPOSED, current_rev: 1 });
    applyPlanProposed(PROPOSED);
    expect(getPlanSnapshot(PID)?.currentRev).toBe(3);
    expect(getPlanSnapshot(PID)?.steps[0].objective).toBe("latest confirmed");
  });

  it("does not assign a known revision to replacement steps from an unversioned snapshot", () => {
    applyPlanProposed({ ...PROPOSED, current_rev: 1 });
    applyPlanSnapshot({ plan_id: PID, steps: [{ ...PROPOSED.steps[0], objective: "unknown version" }] });
    expect(getPlanSnapshot(PID)?.currentRev).toBeUndefined();
  });
});

describe("plan-store — applyPlanProposed", () => {
  it("seeds the plan with steps defaulting to pending + maps wire fields", () => {
    applyPlanProposed(PROPOSED);
    const p = getPlanSnapshot(PID)!;
    expect(p.title).toBe("对青松医疗集团做尽职调查");
    expect(p.status).toBe("proposed");
    expect(p.steps).toHaveLength(3);
    expect(p.steps[0]).toMatchObject({
      id: "s1",
      agent: "compliance-analyst",
      needsCorpus: true,
      status: "pending",
    });
    expect(p.steps[2].dependsOn).toEqual(["s1", "s2"]);
    expect(p.steps[2].level).toBe(1);
  });
});

describe("plan-store — step lifecycle", () => {
  it("step_started marks the given ids running; others untouched", () => {
    applyPlanProposed(PROPOSED);
    applyPlanStepStarted(PID, ["s1", "s2"]);
    const p = getPlanSnapshot(PID)!;
    expect(p.steps.find((s) => s.id === "s1")!.status).toBe("running");
    expect(p.steps.find((s) => s.id === "s2")!.status).toBe("running");
    expect(p.steps.find((s) => s.id === "s3")!.status).toBe("pending");
  });

  it("step_done / step_failed / step_blocked transition the right step", () => {
    applyPlanProposed(PROPOSED);
    applyPlanStepDone(PID, "s1");
    applyPlanStepFailed(PID, "s2", "tool_exception");
    applyPlanStepBlocked(PID, "s3", "blocked", "needs_corpus", "room empty");
    const p = getPlanSnapshot(PID)!;
    expect(p.steps.find((s) => s.id === "s1")!.status).toBe("done");
    const s2 = p.steps.find((s) => s.id === "s2")!;
    expect(s2.status).toBe("failed");
    expect(s2.errorKind).toBe("tool_exception");
    const s3 = p.steps.find((s) => s.id === "s3")!;
    expect(s3.status).toBe("blocked");
    expect(s3.errorKind).toBe("needs_corpus");
    expect(s3.reason).toBe("room empty");
  });

  it("step_blocked status='skipped' maps to skipped", () => {
    applyPlanProposed(PROPOSED);
    applyPlanStepBlocked(PID, "s1", "skipped", "upstream_failed", "");
    expect(getPlanSnapshot(PID)!.steps[0].status).toBe("skipped");
  });

  it("mutators on an unknown plan are no-ops (defensive)", () => {
    applyPlanStepStarted("nope", ["s1"]);
    applyPlanStepDone("nope", "s1");
    expect(getPlanSnapshot("nope")).toBeUndefined();
  });
});

describe("plan-store — plan status + snapshot + dismiss", () => {
  it("applyPlanStatus updates the plan-level status", () => {
    applyPlanProposed(PROPOSED);
    applyPlanStatus(PID, "running");
    expect(getPlanSnapshot(PID)!.status).toBe("running");
    applyPlanStatus(PID, "completed");
    expect(getPlanSnapshot(PID)!.status).toBe("completed");
  });

  it("applyPlanSnapshot (dev REST shape) replaces steps + status, keeps title", () => {
    applyPlanProposed(PROPOSED);
    applyPlanSnapshot({
      plan_id: PID,
      plan: { id: PID, status: "blocked" },
      steps: [
        { id: "s1", agent: "compliance-analyst", objective: "法律", level: 0, status: "done", depends_on: [], needs_corpus: true, error_kind: null },
        { id: "s3", agent: "canvas-writer", objective: "汇总", level: 1, status: "pending", depends_on: ["s1"], needs_corpus: false, error_kind: null },
      ],
    });
    const p = getPlanSnapshot(PID)!;
    expect(p.status).toBe("blocked");
    expect(p.title).toBe("对青松医疗集团做尽职调查"); // preserved from proposed
    expect(p.steps).toHaveLength(2);
    expect(p.steps[0].status).toBe("done");
  });

  it("dismissPlan removes the plan", () => {
    applyPlanProposed(PROPOSED);
    dismissPlan(PID);
    expect(getPlanSnapshot(PID)).toBeUndefined();
  });

  it("dismissPlan blocks late reconcile and replay snapshots in the same page", () => {
    applyPlanProposed(PROPOSED);
    dismissPlan(PID);
    applyPlanSnapshot({
      plan_id: PID,
      plan: { id: PID, status: "running" },
      steps: PROPOSED.steps,
    });
    applyPlanProposed(PROPOSED);
    expect(getPlanSnapshot(PID)).toBeUndefined();
  });
});

describe("plan-store — A(可编辑) local edit mutators", () => {
  it("editStepObjective changes one step's objective", () => {
    applyPlanProposed(PROPOSED);
    editStepObjective(PID, "s1", "改后的目标");
    expect(getPlanSnapshot(PID)!.steps.find((s) => s.id === "s1")!.objective).toBe("改后的目标");
  });

  it("toggleStepIncluded flips included (default true → false → true)", () => {
    applyPlanProposed(PROPOSED);
    toggleStepIncluded(PID, "s2");
    expect(getPlanSnapshot(PID)!.steps.find((s) => s.id === "s2")!.included).toBe(false);
    toggleStepIncluded(PID, "s2");
    expect(getPlanSnapshot(PID)!.steps.find((s) => s.id === "s2")!.included).toBe(true);
  });

  it("deleteStep removes the step AND strips it from others' dependsOn", () => {
    applyPlanProposed(PROPOSED);
    deleteStep(PID, "s1");
    const p = getPlanSnapshot(PID)!;
    expect(p.steps.find((s) => s.id === "s1")).toBeUndefined();
    // s3 depended on [s1, s2] → s1 stripped
    expect(p.steps.find((s) => s.id === "s3")!.dependsOn).toEqual(["s2"]);
  });

  it("reorderSteps moves a step before the target", () => {
    applyPlanProposed(PROPOSED);
    // order s1,s2,s3 → move s2 before s1 → s2,s1,s3
    reorderSteps(PID, "s2", "s1");
    expect(getPlanSnapshot(PID)!.steps.map((s) => s.id)).toEqual(["s2", "s1", "s3"]);
  });
});

// ── 2026-07 planning-ux-reliability: replay never clobbers ──────────────
// F5 hydration replayTurnEvents are concurrent and unordered across turns: execution-turn plan_*/step_*
// may arrive first; a later proposal must never regress advanced state to proposed.
describe("applyPlanProposed replay no-clobber", () => {
  afterEach(() => _resetAllPlansForTesting());

  it("does not reset an advanced plan back to proposed", () => {
    applyPlanProposed(PROPOSED);
    applyPlanStatus(PID, "running");
    applyPlanStepStarted(PID, ["s1", "s2"]);
    applyPlanStepDone(PID, "s1");
    applyPlanProposed(PROPOSED); // Late proposal in unordered replay.
    const p = getPlanSnapshot(PID)!;
    expect(p.status).toBe("running");
    expect(p.steps.find((s) => s.id === "s1")!.status).toBe("done");
  });

  it("still re-seeds when plan is absent or proposed", () => {
    applyPlanProposed(PROPOSED);
    expect(getPlanSnapshot(PID)!.status).toBe("proposed");
    applyPlanProposed({ ...PROPOSED, title: "更新标题" });
    expect(getPlanSnapshot(PID)!.title).toBe("更新标题");
  });
});

// ── graph-mode batch 1: criteria/verify passthrough and snapshot retention ────────

describe("graph-mode verify/criteria", () => {
  const PID = "plan_gm1";

  it("normalizes success_criteria + verify from proposal/REST shapes", () => {
    applyPlanProposed({
      plan_id: PID,
      title: "t",
      steps: [
        { id: "s1", agent: "compute", objective: "o", level: 0, depends_on: [], success_criteria: ["含季度数字"] },
      ],
    });
    expect(getPlanSnapshot(PID)?.steps[0]?.successCriteria).toEqual(["含季度数字"]);
  });

  it("applyPlanStepDone carries verify verdict; snapshot merge preserves it", () => {
    applyPlanProposed({
      plan_id: PID,
      title: "t",
      steps: [{ id: "s1", agent: "compute", objective: "o", level: 0, depends_on: [], success_criteria: ["c"] }],
    });
    applyPlanStepDone(PID, "s1", { verdict: "partial", unmet: ["c"] });
    expect(getPlanSnapshot(PID)?.steps[0]?.verify?.verdict).toBe("partial");
    // Snapshot lacks verify/criteria (old backend shape) → retain prev, never clear it (lesson from feasibility).
    applyPlanSnapshot({
      plan_id: PID,
      plan_status: "running",
      steps: [{ id: "s1", agent: "compute", objective: "o", level: 0, depends_on: [], status: "done" }],
    });
    expect(getPlanSnapshot(PID)?.steps[0]?.verify?.verdict).toBe("partial");
    expect(getPlanSnapshot(PID)?.steps[0]?.successCriteria).toEqual(["c"]);
  });

  it("rejects malformed verify payloads", () => {
    applyPlanProposed({
      plan_id: PID,
      title: "t",
      steps: [{ id: "s1", agent: "compute", objective: "o", level: 0, depends_on: [] }],
    });
    applyPlanStepDone(PID, "s1", { verdict: "banana" });
    expect(getPlanSnapshot(PID)?.steps[0]?.verify ?? null).toBeNull();
  });

  // ── Batch 3: superseded terminal-state guard (review R2-F7) ──
  it("批3: replay 的旧 step_failed/step_started 不得复活 superseded 死行", () => {
    applyPlanProposed({
      plan_id: PID,
      title: "t",
      steps: [{ id: "s1", agent: "compute", objective: "o", level: 0, depends_on: [] }],
    });
    // Authoritative snapshot arrives first: s1 has been replaced by revision.
    applyPlanSnapshot({
      plan_id: PID,
      plan_status: "blocked",
      steps: [
        { id: "s1", agent: "compute", objective: "o", level: 0, depends_on: [], status: "superseded", superseded_by: "s_rev" },
        { id: "s_rev", agent: "consultant", objective: "修订步", level: 0, depends_on: [], status: "pending", origin: "revision" },
      ],
    });
    expect(getPlanSnapshot(PID)?.steps[0]?.status).toBe("superseded");
    expect(getPlanSnapshot(PID)?.steps[0]?.supersededBy).toBe("s_rev");
    // Old execution-turn events arrive later (F5 replay unordered across turns) → dead row must remain unchanged.
    applyPlanStepFailed(PID, "s1", "TimeoutError");
    expect(getPlanSnapshot(PID)?.steps[0]?.status).toBe("superseded");
    applyPlanStepStarted(PID, ["s1"]);
    expect(getPlanSnapshot(PID)?.steps[0]?.status).toBe("superseded");
    applyPlanStepDone(PID, "s1");
    expect(getPlanSnapshot(PID)?.steps[0]?.status).toBe("superseded");
  });

  it("批3: snapshot 缺 superseded_by 字段(旧后端)时 preserve 不清", () => {
    applyPlanProposed({
      plan_id: PID,
      title: "t",
      steps: [{ id: "s1", agent: "compute", objective: "o", level: 0, depends_on: [] }],
    });
    applyPlanSnapshot({
      plan_id: PID,
      plan_status: "blocked",
      steps: [{ id: "s1", agent: "compute", objective: "o", level: 0, depends_on: [], status: "superseded", superseded_by: "s_rev" }],
    });
    applyPlanSnapshot({
      plan_id: PID,
      plan_status: "blocked",
      steps: [{ id: "s1", agent: "compute", objective: "o", level: 0, depends_on: [], status: "superseded" }],
    });
    expect(getPlanSnapshot(PID)?.steps[0]?.supersededBy).toBe("s_rev");
  });

  it("批3: REST error 字段回填 reason(F5 后 failed 步详情不只剩 kind 裸串)", () => {
    applyPlanProposed({
      plan_id: PID,
      title: "t",
      steps: [{ id: "s1", agent: "compute", objective: "o", level: 0, depends_on: [] }],
    });
    applyPlanSnapshot({
      plan_id: PID,
      plan_status: "blocked",
      steps: [{ id: "s1", agent: "compute", objective: "o", level: 0, depends_on: [], status: "failed", error: "验收未达: 缺数值", error_kind: "criteria_unmet" }],
    });
    expect(getPlanSnapshot(PID)?.steps[0]?.reason).toBe("验收未达: 缺数值");
  });
});

describe("M3 exact Agent choice", () => {
  const PID = "plan_exact_agent";
  const choice = {
    schema_version: 1,
    role_id: "image",
    candidate_id: `lrc_${"a".repeat(32)}`,
    reason_code: "task_match",
    agent_id: "system:marketing-analyst",
    runtime_profile_name: "marketing-analyst",
    assignment_id: null,
    revision_ref: "arv_image",
    release_id: null,
    behavior_fingerprint: `sha256:${"b".repeat(64)}`,
  };

  it("keeps the opaque exact identity through proposal and legacy snapshots", () => {
    applyPlanProposed({
      plan_id: PID,
      title: "图片计划",
      steps: [{
        id: "s1",
        agent: "marketing-analyst",
        objective: "生成主视觉",
        level: 0,
        depends_on: [],
        agent_choice: choice,
      }],
    });
    expect(getPlanSnapshot(PID)?.steps[0]?.agentChoice).toEqual(choice);

    applyPlanSnapshot({
      plan_id: PID,
      plan_status: "proposed",
      steps: [{ id: "s1", agent: "marketing-analyst", objective: "生成主视觉", level: 0, depends_on: [] }],
    });
    expect(getPlanSnapshot(PID)?.steps[0]?.agentChoice).toEqual(choice);
  });

  it("honors an explicit null from the authoritative server snapshot", () => {
    applyPlanProposed({
      plan_id: PID,
      title: "图片计划",
      steps: [{
        id: "s1",
        agent: "marketing-analyst",
        objective: "生成主视觉",
        level: 0,
        depends_on: [],
        agent_choice: choice,
      }],
    });
    applyPlanSnapshot({
      plan_id: PID,
      plan_status: "proposed",
      steps: [{
        id: "s1",
        agent: "compute",
        objective: "改成普通分析",
        level: 0,
        depends_on: [],
        agent_choice: null,
      }],
    });
    expect(getPlanSnapshot(PID)?.steps[0]?.agentChoice).toBeNull();
  });
});
