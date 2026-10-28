/**
 * V2.8 L · Trace-L reducer unit tests.
 *
 * Cover heuristic plan matching + subagent_id backfill (CTO review fix) + render-time nested attachment.
 */

import { describe, expect, test } from "vitest";

import {
  attachSubagentChildren,
  backfillSpawnSubagentId,
  matchPlanStepForTool,
  planFinish,
  planStepResolved,
  planStepRunning,
} from "./trace-l-reducer";
import type { PlanPreview, PlanPreviewStep, TraceStep } from "./types";

// ─────────────────────────────────────────────────────────────
// helpers
// ─────────────────────────────────────────────────────────────

function makePlan(stepsRaw: Array<Partial<PlanPreviewStep>>): PlanPreview {
  return {
    steps: stepsRaw.map((s, i) => ({
      id: s.id ?? `s${i + 1}`,
      verb: s.verb ?? `verb${i}`,
      hint: s.hint ?? "",
      etaMs: s.etaMs ?? 1000,
      skillTag: s.skillTag ?? "",
      status: s.status ?? "pending",
      ...s,
    })),
    estimatedDurationMs: 5000,
    startedAt: 0,
    status: "running",
  };
}

function step(p: Partial<TraceStep> & Pick<TraceStep, "id">): TraceStep {
  return {
    title: p.title ?? "x",
    kind: p.kind ?? "running",
    ...p,
  } as TraceStep;
}

// ─────────────────────────────────────────────────────────────
// L1: plan matching.
// ─────────────────────────────────────────────────────────────

describe("matchPlanStepForTool", () => {
  test("跳过 done 步, 匹配第一个 pending 同 skillTag", () => {
    const plan = makePlan([
      { skillTag: "bi.ask_question", status: "done" },
      { skillTag: "bi.ask_question", status: "pending" },
      { skillTag: "bi.ask_question", status: "pending" },
    ]);
    expect(matchPlanStepForTool(plan, "bi.ask_question")).toBe(1);
  });

  test("compute alias → skill.invoke", () => {
    const plan = makePlan([{ skillTag: "compute" }]);
    expect(matchPlanStepForTool(plan, "skill.invoke")).toBe(0);
  });

  test("canvas. prefix alias", () => {
    const plan = makePlan([{ skillTag: "canvas" }]);
    expect(matchPlanStepForTool(plan, "canvas.add_section")).toBe(0);
  });

  test("无匹配 → -1", () => {
    const plan = makePlan([{ skillTag: "bi.ask_question" }]);
    expect(matchPlanStepForTool(plan, "skill.invoke")).toBe(-1);
  });
});

describe("planStepRunning", () => {
  test("匹配的 pending → running", () => {
    const plan = makePlan([
      { skillTag: "bi.ask_question", status: "pending" },
      { skillTag: "compute", status: "pending" },
    ]);
    const out = planStepRunning(plan, "bi.ask_question");
    expect(out.steps[0].status).toBe("running");
    expect(out.steps[1].status).toBe("pending");
  });

  test("plan.status=done 时不动", () => {
    const plan = { ...makePlan([{ skillTag: "x" }]), status: "done" as const };
    const out = planStepRunning(plan, "x");
    expect(out).toBe(plan);  // Same reference (helper returns early).
  });
});

describe("planStepResolved", () => {
  test("running step → done + actualMs", () => {
    const plan = makePlan([
      { skillTag: "bi.ask_question", status: "done" },
      { skillTag: "compute", status: "running" },
      { skillTag: "canvas", status: "pending" },
    ]);
    const out = planStepResolved(plan, true, 1234, null);
    expect(out.steps[1].status).toBe("done");
    expect(out.steps[1].actualMs).toBe(1234);
    expect(out.steps[2].status).toBe("pending");  // Unaffected.
  });

  test("failed → 后续 pending 全 skipped", () => {
    const plan = makePlan([
      { status: "running" },
      { status: "pending" },
      { status: "pending" },
    ]);
    const out = planStepResolved(plan, false, 30000, "BI 超时");
    expect(out.steps[0].status).toBe("failed");
    expect(out.steps[0].errMsg).toBe("BI 超时");
    expect(out.steps[1].status).toBe("skipped");
    expect(out.steps[2].status).toBe("skipped");
  });
});

describe("planFinish", () => {
  test("turn done → 所有 pending 改 skipped, plan 改 done", () => {
    const plan = makePlan([
      { status: "done" },
      { status: "running" },  // Keep unresolved running steps too (independent of reducer call order).
      { status: "pending" },
    ]);
    const out = planFinish(plan, 1700, "done");
    expect(out.status).toBe("done");
    expect(out.finishedAt).toBe(1700);
    expect(out.steps[0].status).toBe("done");
    expect(out.steps[2].status).toBe("skipped");
  });
});

// ─────────────────────────────────────────────────────────────
// L2: subagent backfill (CTO review bug fix).
// ─────────────────────────────────────────────────────────────

describe("backfillSpawnSubagentId", () => {
  test("first child stamps the most-recent unlinked running spawn step", () => {
    const steps: TraceStep[] = [
      step({ id: "tool_X", toolName: "subagent.spawn", kind: "running" }),
    ];
    const out = backfillSpawnSubagentId(steps, "bi.ask_question", "sub-uuid-A");
    expect(out[0].subagentId).toBe("sub-uuid-A");
  });

  test("second child of same subagent_id 不再 backfill", () => {
    const steps: TraceStep[] = [
      step({ id: "tool_X", toolName: "subagent.spawn", kind: "running", subagentId: "sub-uuid-A" }),
      step({ id: "tool_Y", toolName: "bi.ask_question", subagentId: "sub-uuid-A" }),
    ];
    const out = backfillSpawnSubagentId(steps, "bi.fetch_rows", "sub-uuid-A");
    // Unchanged (alreadyLinked); return an equivalent array.
    expect(out[0].subagentId).toBe("sub-uuid-A");
  });

  test("subagent.spawn 自身不触发 backfill", () => {
    const steps: TraceStep[] = [
      step({ id: "tool_X", toolName: "subagent.spawn", kind: "running" }),
    ];
    const out = backfillSpawnSubagentId(steps, "subagent.spawn", "sub-uuid-A");
    expect(out[0].subagentId).toBeUndefined();
  });

  test("已 done 的 spawn step 不被 backfill (只挑 running 的)", () => {
    const steps: TraceStep[] = [
      step({ id: "tool_X", toolName: "subagent.spawn", kind: "success" }),
      step({ id: "tool_Y", toolName: "subagent.spawn", kind: "running" }),
    ];
    const out = backfillSpawnSubagentId(steps, "bi.ask_question", "sub-uuid-Y");
    expect(out[0].subagentId).toBeUndefined();  // Old spawn remains unchanged.
    expect(out[1].subagentId).toBe("sub-uuid-Y");
  });

  test("无 subagentId 直接返原 array", () => {
    const steps: TraceStep[] = [
      step({ id: "tool_X", toolName: "bi.ask_question" }),
    ];
    const out = backfillSpawnSubagentId(steps, "bi.ask_question", undefined);
    expect(out).toHaveLength(1);
    expect(out[0].subagentId).toBeUndefined();
  });
});

// ─────────────────────────────────────────────────────────────
// Render-time nested attachment.
// ─────────────────────────────────────────────────────────────

describe("attachSubagentChildren", () => {
  test("把 child 抽出 + 挂到 spawn step.detail.__children", () => {
    const steps: TraceStep[] = [
      step({ id: "tool_X", toolName: "subagent.spawn", kind: "running", subagentId: "sub-A" }),
      step({ id: "tool_Y", toolName: "bi.ask_question", subagentId: "sub-A" }),
      step({ id: "tool_Z", toolName: "bi.fetch_rows", subagentId: "sub-A" }),
    ];
    const out = attachSubagentChildren(steps);
    expect(out).toHaveLength(1);  // Child steps were extracted.
    const detail = out[0].detail as { __children: TraceStep[] };
    expect(detail.__children).toHaveLength(2);
    expect(detail.__children[0].id).toBe("tool_Y");
  });

  test("没绑定 subagentId 的 spawn step 不挂 children (即使有同 id 子 step) — 防错配", () => {
    const steps: TraceStep[] = [
      step({ id: "tool_X", toolName: "subagent.spawn", kind: "running" }),  // No subagentId.
      step({ id: "tool_Y", toolName: "bi.ask_question", subagentId: "sub-A" }),
    ];
    const out = attachSubagentChildren(steps);
    expect(out).toHaveLength(1);  // Children extracted (only spawn remains at top level).
    expect(out[0].detail).toBeUndefined();  // But spawn has no subagentId, so children were not attached → no __children.
  });

  test("无 children 时不动 spawn step", () => {
    const steps: TraceStep[] = [
      step({ id: "tool_X", toolName: "subagent.spawn", kind: "running", subagentId: "sub-A" }),
    ];
    const out = attachSubagentChildren(steps);
    expect(out).toHaveLength(1);
    expect(out[0].detail).toBeUndefined();
  });
});
