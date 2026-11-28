// @vitest-environment jsdom
/**
 * Chat running state R1.5–R1.8: one group per agent appearance, placed where Lead dispatched it; protocol
 * steps are not lines; identical failures merge; a running card keeps three finished lines per group and
 * shrinks each helper to its current line when more than two run at once (P1).
 */
import { describe, expect, it } from "vitest";

import { activeHelperCount, buildRunGroups, linesOf, windowRunGroup } from "./run-groups";
import { rowOf } from "./running-band-model";
import type { StepState, StepView } from "./types";

function sv(partial: Partial<StepView> & { stepId: string }): StepView {
  return {
    parentStepId: null,
    concurrencyGroupId: null,
    stepType: "tool_run",
    toolName: "bi_query",
    headerZh: "查询数据",
    state: "done" as StepState,
    ...partial,
  };
}

function spawn(stepId: string, peer: string, extra: Partial<StepView> = {}): StepView {
  return sv({ stepId, stepType: "subagent", toolName: "subagent_spawn", headerZh: "派给助手", subagentId: "LEAD", peerProseKey: peer, ...extra });
}

const failed = (stepId: string, extra: Partial<StepView> = {}): StepView => sv({
  stepId,
  toolName: "kb_search",
  headerZh: "检索资料",
  label: "pptx",
  state: "failed",
  error: { kind: "permanent", uiKind: "permanent", message: "not found", code: "skill_unavailable" },
  ...extra,
});

describe("buildRunGroups", () => {
  it("places each helper where Lead dispatched it, between Lead's own runs", () => {
    const groups = buildRunGroups({
      steps: [
        sv({ stepId: "l1", subagentId: "LEAD" }),
        spawn("s1", "P1", { state: "done", startedAtMs: 1_000, durationMs: 50_000 }),
        sv({ stepId: "p1a", subagentId: "P1", toolName: "python_sandbox", headerZh: "运行代码" }),
        sv({ stepId: "l2", subagentId: "LEAD", toolName: "kb_search", headerZh: "检索资料" }),
      ],
      peerNames: { P1: "compute" },
      peerTerminalById: { P1: { status: "succeeded", durationMs: 50_000 } },
    });
    expect(groups.map((g) => [g.kind, g.lines.map((l) => l.step.stepId)])).toEqual([
      ["lead", ["l1"]],
      ["helper", ["p1a"]],
      ["lead", ["l2"]],
    ]);
  });

  it("tells two helpers of the same kind apart by task when they overlap, and counts a later dispatch", () => {
    const groups = buildRunGroups({
      steps: [
        spawn("s1", "P1", { label: "销售额", startedAtMs: 1_000, durationMs: 25_000 }),
        spawn("s2", "P2", { label: "订单数", startedAtMs: 1_100, durationMs: 51_000 }),
        spawn("s3", "P3", { label: "汇总", startedAtMs: 90_000, durationMs: 30_000 }),
      ],
      peerNames: { P1: "compute", P2: "compute", P3: "compute" },
      peerTerminalById: {
        P1: { status: "succeeded", durationMs: 25_000 },
        P2: { status: "succeeded", durationMs: 51_000 },
        P3: { status: "succeeded", durationMs: 30_000 },
      },
    });
    expect(groups.map((g) => [g.task, g.repeat])).toEqual([["销售额", null], ["订单数", null], [null, 3]]);
  });

  it("a second, later dispatch of the same helper reads as its second run", () => {
    const groups = buildRunGroups({
      steps: [
        spawn("s1", "P1", { startedAtMs: 0, durationMs: 158_000 }),
        spawn("s2", "P2", { startedAtMs: 198_000, durationMs: 104_000 }),
      ],
      peerNames: { P1: "compute", P2: "compute" },
      peerTerminalById: { P1: { status: "succeeded", durationMs: 158_000 }, P2: { status: "succeeded", durationMs: 104_000 } },
    });
    expect(groups.map((g) => [g.task, g.repeat])).toEqual([[null, null], [null, 2]]);
  });

  it("carries a returned helper's caveats only on a warning return", () => {
    const groups = buildRunGroups({
      steps: [spawn("s1", "P1")],
      peerNames: { P1: "compute" },
      peerTerminalById: { P1: { status: "succeeded_with_warnings", caveats: ["数据源没有「订单数」字段，改用了销售量（件）"] } },
    });
    expect(groups[0].caveats).toEqual(["数据源没有「订单数」字段，改用了销售量（件）"]);
  });
});

describe("linesOf", () => {
  it("never makes protocol steps into lines, and reports the one running", () => {
    const { lines, protocol, stepCount } = linesOf([
      sv({ stepId: "a" }),
      sv({ stepId: "r1", toolName: "subagent_return", state: "failed", error: { kind: "transient", message: "", code: "tool_delivery_review_required" } }),
      sv({ stepId: "r2", toolName: "subagent_return", state: "cancelled" }),
      sv({ stepId: "r3", toolName: "subagent_return", state: "active" }),
    ]);
    expect(lines.map((l) => l.step.stepId)).toEqual(["a"]);
    expect(protocol).toBe("return");
    expect(stepCount).toBe(1);
  });

  it("merges identical consecutive failures and keeps a different failure apart", () => {
    const { lines, stepCount } = linesOf([
      failed("f1"), failed("f2"), failed("f3"), failed("f4"), failed("f5"),
      failed("g1", { label: "xlsx" }),
    ]);
    expect(lines.map((l) => [l.key, l.failures])).toEqual([["f1", 5], ["g1", 1]]);
    expect(lines[0].step.stepId).toBe("f5");
    expect(stepCount).toBe(6);
  });

  it("turns a failure followed by the same call succeeding into one retried success", () => {
    const { lines } = linesOf([failed("f1"), failed("f2", { state: "done", error: null })]);
    expect(lines).toHaveLength(1);
    expect(lines[0]).toMatchObject({ key: "f1", failures: 0, retried: 1, tone: "done" });
  });
});

describe("windowRunGroup", () => {
  const many = Array.from({ length: 9 }, (_, i) => sv({ stepId: `p${i}`, subagentId: "P1", label: `q${i}` }));

  it("keeps the last three finished lines and the current one, and counts the rest", () => {
    const steps = [spawn("s1", "P1", { state: "active" }), ...many, sv({ stepId: "now", subagentId: "P1", state: "active" })];
    const [group] = buildRunGroups({ steps, peerNames: { P1: "compute" } });
    const view = windowRunGroup(group, 1);
    expect(view.hiddenBefore).toBe(6);
    expect(view.lines.map((l) => l.step.stepId)).toEqual(["p6", "p7", "p8", "now"]);
    expect(view.compact).toBe(false);
  });

  it("never folds away a failure", () => {
    const steps = [spawn("s1", "P1", { state: "active" }), failed("f1", { subagentId: "P1" }), ...many];
    const [group] = buildRunGroups({ steps, peerNames: { P1: "compute" } });
    expect(windowRunGroup(group, 1).lines.map((l) => l.step.stepId)).toEqual(["f1", "p6", "p7", "p8"]);
  });

  it("shrinks each helper to its current line when more than two run at once (P1)", () => {
    const peers = ["A", "B", "C"];
    const steps = peers.flatMap((peer) => [
      spawn(`s${peer}`, peer, { state: "active" }),
      ...many.map((step) => ({ ...step, stepId: `${peer}-${step.stepId}`, subagentId: peer })),
      sv({ stepId: `${peer}-now`, subagentId: peer, state: "active" }),
    ]);
    const groups = buildRunGroups({ steps, peerNames: { A: "compute", B: "compute", C: "compute" } });
    expect(activeHelperCount(groups)).toBe(3);
    const view = windowRunGroup(groups[0], activeHelperCount(groups));
    expect(view).toMatchObject({ compact: true, hiddenBefore: 0 });
    expect(view.lines.map((l) => l.step.stepId)).toEqual(["A-now"]);
    expect(groups[0].stepCount).toBe(10);
  });
});

describe("dispatch rows", () => {
  it("never shows a dispatch still waiting for its helper's name as Lead's current line", () => {
    const groups = buildRunGroups({
      steps: [sv({ stepId: "q", subagentId: "LEAD" }), spawn("s1", "P1", { state: "active" })],
      peerNames: {},
    });
    expect(groups.flatMap((g) => g.lines.map((l) => l.step.stepId))).toEqual(["q"]);
    expect(groups[0].active).toBeNull();
  });
});

describe("current line wording", () => {
  it("names a running step by its plain name, so 正在{what} never doubles 正在", () => {
    document.documentElement.lang = "zh-CN";
    try {
      const running = sv({ stepId: "r", state: "active", toolName: "agent_message", headerZh: "", label: "" });
      // Positive control: the catalog's active name already starts with 正在 ("正在协作沟通").
      expect(rowOf(running).label.startsWith("正在")).toBe(true);
      const [group] = buildRunGroups({ steps: [running], peerNames: {} });
      expect(group.active?.label).toBeTruthy();
      expect(group.active?.label.startsWith("正在")).toBe(false);
    } finally {
      document.documentElement.lang = "en-US";
    }
  });
});
