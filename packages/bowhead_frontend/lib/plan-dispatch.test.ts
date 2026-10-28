import { afterEach, describe, expect, it } from "vitest";

import { dispatchToTurnStore, _resetNarrativeCursorsForTesting } from "./turn-store-dispatch";
import { _resetAllTurnsForTesting, getTurnSnapshot } from "./turn-store";
import { _resetAllPlansForTesting, getPlanSnapshot } from "./plan-store";

const TURN = "t_plan";
const PID = "plan_1";

const PROPOSED = {
  turn_id: TURN,
  plan_id: PID,
  title: "DD",
  steps: [
    { id: "s1", agent: "compliance-analyst", objective: "法律", level: 0, depends_on: [], needs_corpus: true },
    { id: "s2", agent: "canvas-writer", objective: "汇总", level: 1, depends_on: ["s1"], needs_corpus: false },
  ],
};

afterEach(() => {
  _resetAllTurnsForTesting();
  _resetAllPlansForTesting();
  _resetNarrativeCursorsForTesting();
});

describe("plan dispatch — plan_proposed", () => {
  it("seeds plan-store AND pushes a plan beat onto the proposal turn", () => {
    const claimed = dispatchToTurnStore("plan_proposed", PROPOSED);
    expect(claimed).toBe(true);
    expect(getPlanSnapshot(PID)?.steps).toHaveLength(2);
    const beats = getTurnSnapshot(TURN)?.beats ?? [];
    expect(beats.some((b) => b.kind === "plan" && b.planId === PID)).toBe(true);
  });

  it("dup plan_proposed does not add a second plan beat", () => {
    dispatchToTurnStore("plan_proposed", PROPOSED);
    dispatchToTurnStore("plan_proposed", PROPOSED);
    const beats = getTurnSnapshot(TURN)?.beats ?? [];
    expect(beats.filter((b) => b.kind === "plan").length).toBe(1);
  });
});

describe("plan dispatch — step_started/step_done disambiguation", () => {
  it("plan-scoped step_started (plan_id + step_ids) routes to plan-store, not turn-store", () => {
    dispatchToTurnStore("plan_proposed", PROPOSED);
    const claimed = dispatchToTurnStore("step_started", { plan_id: PID, level: 0, step_ids: ["s1"] });
    expect(claimed).toBe(true);
    expect(getPlanSnapshot(PID)!.steps.find((s) => s.id === "s1")!.status).toBe("running");
    // turn-store must NOT have gained a step (that's the step-list card path)
    expect((getTurnSnapshot(TURN)?.steps ?? []).length).toBe(0);
  });

  it("step-list step_started (card_id) still routes to turn-store, not plan-store", () => {
    dispatchToTurnStore("plan_proposed", PROPOSED);
    const before = getPlanSnapshot(PID)!.steps.map((s) => s.status).join();
    const claimed = dispatchToTurnStore("step_started", {
      card_id: TURN,
      step_id: "tool-1",
      step_type: "tool_run",
      header_zh: "查询数据",
      started_at: "2026-06-24T00:00:00Z",
    });
    expect(claimed).toBe(true);
    expect((getTurnSnapshot(TURN)?.steps ?? []).length).toBe(1);
    // plan steps untouched
    expect(getPlanSnapshot(PID)!.steps.map((s) => s.status).join()).toBe(before);
  });

  it("plan-scoped step_done (plan_id, no card_id) routes to plan-store", () => {
    dispatchToTurnStore("plan_proposed", PROPOSED);
    dispatchToTurnStore("step_started", { plan_id: PID, level: 0, step_ids: ["s1"] });
    dispatchToTurnStore("step_done", { plan_id: PID, step_id: "s1", digest: "ok" });
    expect(getPlanSnapshot(PID)!.steps.find((s) => s.id === "s1")!.status).toBe("done");
  });
});

describe("plan dispatch — block / fail / plan-level", () => {
  it("step_blocked + plan_blocked update plan-store", () => {
    dispatchToTurnStore("plan_proposed", PROPOSED);
    dispatchToTurnStore("step_blocked", {
      plan_id: PID, step_id: "s1", status: "blocked", error_kind: "needs_corpus", reason: "empty",
    });
    dispatchToTurnStore("plan_blocked", { plan_id: PID, blocked_step_ids: ["s1"] });
    const p = getPlanSnapshot(PID)!;
    expect(p.status).toBe("blocked");
    expect(p.steps.find((s) => s.id === "s1")!.errorKind).toBe("needs_corpus");
  });

  it("plan_blocked records the blocked plan on its execution turn, live and replayed alike", () => {
    dispatchToTurnStore("plan_proposed", PROPOSED);
    dispatchToTurnStore("plan_blocked", { turn_id: "t_exec", plan_id: PID, blocked_step_ids: [] }, "live", "t_exec");
    expect(getTurnSnapshot("t_exec")?.blockedPlanId).toBe(PID);
    _resetAllTurnsForTesting();
    dispatchToTurnStore("plan_blocked", { plan_id: PID, blocked_step_ids: ["s1"] }, "replay", "t_exec");
    expect(getTurnSnapshot("t_exec")?.blockedPlanId).toBe(PID);
    expect(getTurnSnapshot(TURN)?.blockedPlanId).toBeUndefined();
  });

  it("plan_completed sets completed status", () => {
    dispatchToTurnStore("plan_proposed", PROPOSED);
    dispatchToTurnStore("plan_completed", { plan_id: PID });
    expect(getPlanSnapshot(PID)!.status).toBe("completed");
  });
});

describe("★step_failed 必须把 verify 带进 store（enforce 前置②的真缝）", () => {
  // Backend criteria_unmet step_failed already included unmet items (plan_driver
  // `_verify_event_field`), but FE dispatch discarded field; switching to enforce left
  // user only a truncated system error. Component tests feed props directly and miss seam,
  // so enforce at dispatch layer.
  it("verify 落到 step 上，unmet 逐条保留", () => {
    dispatchToTurnStore("plan_proposed", PROPOSED);
    dispatchToTurnStore("step_failed", {
      plan_id: PID,
      step_id: "s1",
      error_kind: "criteria_unmet",
      verify: { verdict: "fail", unmet: ["缺少同业口径", "未标注来源"], note: "两条都没满足" },
    });
    const step = getPlanSnapshot(PID)?.steps.find((s) => s.id === "s1");
    expect(step?.status).toBe("failed");
    expect(step?.errorKind).toBe("criteria_unmet");
    expect(step?.verify?.verdict).toBe("fail");
    expect(step?.verify?.unmet).toEqual(["缺少同业口径", "未标注来源"]);
  });

  it("非 criteria 失败无 verify 时不炸，也不伪造", () => {
    dispatchToTurnStore("plan_proposed", PROPOSED);
    dispatchToTurnStore("step_failed", { plan_id: PID, step_id: "s1", error_kind: "TimeoutError" });
    const step = getPlanSnapshot(PID)?.steps.find((s) => s.id === "s1");
    expect(step?.status).toBe("failed");
    expect(step?.verify ?? null).toBeNull();
  });

  it("畸形 verify（verdict 不在词表）被拒，不写脏值", () => {
    dispatchToTurnStore("plan_proposed", PROPOSED);
    dispatchToTurnStore("step_failed", {
      plan_id: PID,
      step_id: "s1",
      error_kind: "criteria_unmet",
      verify: { verdict: "bogus", unmet: ["x"] },
    });
    expect(getPlanSnapshot(PID)?.steps.find((s) => s.id === "s1")?.verify ?? null).toBeNull();
  });
});

describe("★step_failed 还得把 reason 带进 store（116 dogfood 2026-08-15 的现场）", () => {
  // **Same event/problem** as verify above: backend storage always had full readable explanation,
  // but event omitted and dispatch ignored it; live card showed blank after “Failed:”,
  // full text appeared **only after F5**. Actual device text:
  //   Failed:                              ← live
  //   Failed: 知识库中无《供应商合同》文档…  ← after F5
  // Adjacent step_blocked always carried reason; only failed branch was blind.
  it("reason 落到 step 上（突变：dispatch 不传第 5 个参数 → 本条红）", () => {
    dispatchToTurnStore("plan_proposed", PROPOSED);
    dispatchToTurnStore("step_failed", {
      plan_id: PID,
      step_id: "s1",
      error_kind: "domain_mismatch",
      reason: "知识库中无《供应商合同》文档，也无任何含供应商名称/账期的内容可提取。",
    });
    const step = getPlanSnapshot(PID)?.steps.find((s) => s.id === "s1");
    expect(step?.status).toBe("failed");
    expect(step?.reason).toBe("知识库中无《供应商合同》文档，也无任何含供应商名称/账期的内容可提取。");
  });

  it("没带 reason 时不伪造，也不擦掉已有的", () => {
    dispatchToTurnStore("plan_proposed", PROPOSED);
    dispatchToTurnStore("step_failed", { plan_id: PID, step_id: "s1", error_kind: "TimeoutError" });
    expect(getPlanSnapshot(PID)?.steps.find((s) => s.id === "s1")?.reason ?? null).toBeNull();
  });

  it("reason 与 verify 同时带时互不覆盖", () => {
    dispatchToTurnStore("plan_proposed", PROPOSED);
    dispatchToTurnStore("step_failed", {
      plan_id: PID,
      step_id: "s1",
      error_kind: "criteria_unmet",
      reason: "验收未达: 缺少同业口径",
      verify: { verdict: "fail", unmet: ["缺少同业口径"] },
    });
    const step = getPlanSnapshot(PID)?.steps.find((s) => s.id === "s1");
    expect(step?.reason).toBe("验收未达: 缺少同业口径");
    expect(step?.verify?.unmet).toEqual(["缺少同业口径"]);
  });
});

describe("Auto 出的计划一开始就是执行中（2026-08-15 用户拍板）", () => {
  // Plan mode promises plan first, await approval before execution; that promise belongs to **that mode**;
  // Auto plans only when needed, never promises approval blocking. Backend executes in same turn after Auto card,
  // so rendering awaiting approval creates a useless Approve button.
  it("auto_run=true → status 直接是 running（不出批准按钮的前提）", () => {
    dispatchToTurnStore("plan_proposed", { ...PROPOSED, auto_run: true });
    expect(getPlanSnapshot(PID)?.status).toBe("running");
  });

  it("负对照：auto_run=false / 缺省 → 仍是 proposed（显式 Plan 模式要等批准）", () => {
    dispatchToTurnStore("plan_proposed", { ...PROPOSED, auto_run: false });
    expect(getPlanSnapshot(PID)?.status).toBe("proposed");
  });

  it("负对照：后端显式给了 status 时以它为准，auto_run 不越权", () => {
    dispatchToTurnStore("plan_proposed", { ...PROPOSED, status: "proposed", auto_run: true });
    expect(getPlanSnapshot(PID)?.status).toBe("proposed");
  });
});

describe("out_of_domain verdict 必须穿过 FE 契约（2026-08-16）", () => {
  // ⚠ `normalizeVerify` is an **allowlist**: unknown verdict discards **entire verify object**,
  // making badge vanish with no user feedback. New backend verdicts must be added here in same change.
  it("out_of_domain 落到 step 上，不被白名单吞掉", () => {
    dispatchToTurnStore("plan_proposed", PROPOSED);
    dispatchToTurnStore("step_done", {
      plan_id: PID,
      step_id: "s1",
      verify: { verdict: "out_of_domain", unmet: [], note: "本工作区没有冷链温控数据" },
    });
    const step = getPlanSnapshot(PID)?.steps.find((s) => s.id === "s1");
    // Mutation: remove "out_of_domain" from allowlist → verify becomes null, this fails.
    expect(step?.verify?.verdict).toBe("out_of_domain");
    expect(step?.verify?.note).toBe("本工作区没有冷链温控数据");
  });

  it("负对照：真正无效的 verdict 仍被拒，不写脏值", () => {
    dispatchToTurnStore("plan_proposed", PROPOSED);
    dispatchToTurnStore("step_done", { plan_id: PID, step_id: "s1", verify: { verdict: "bogus", unmet: [] } });
    expect(getPlanSnapshot(PID)?.steps.find((s) => s.id === "s1")?.verify ?? null).toBeNull();
  });
});
