/**
 * Batch 5 pure graph-layout logic. Pin design review P1-1: resolve supersede chains;
 * design's single-hop resolution silently loses edges after a second revision.
 */

import { describe, expect, it } from "vitest";

import {
  buildGraphModel,
  isBlockedByUpstream,
  resolveDependency,
  brokenCount,
  needsHorizontalScroll,
  stepperLinkState,
  stepperRingState,
  shouldRenderGraph,
  verifyGlyphKey,
} from "./graph-layout";
import type { PlanStepView } from "./types";

function step(id: string, over: Partial<PlanStepView> = {}): PlanStepView {
  return {
    id,
    agent: "compute",
    objective: "obj " + id,
    level: 0,
    dependsOn: [],
    needsCorpus: false,
    status: "pending",
    included: true,
    ...over,
  };
}

describe("resolveDependency — supersede 沿链", () => {
  const build = (steps: PlanStepView[]) => ({
    byId: new Map(steps.map((s) => [s.id, s])),
    visible: new Set(steps.filter((s) => s.status !== "superseded").map((s) => s.id)),
  });

  it("依赖指向活步 → 原样返回", () => {
    const steps = [step("a"), step("b")];
    const { byId, visible } = build(steps);
    expect(resolveDependency("a", byId, visible)).toBe("a");
  });

  it("一次修订：依赖指向旧步 → 解析到替代步", () => {
    const steps = [step("a", { status: "superseded", supersededBy: "a2" }), step("a2")];
    const { byId, visible } = build(steps);
    expect(resolveDependency("a", byId, visible)).toBe("a2");
  });

  it("★二次修订：a→a2→a3，依赖仍写最老的 a → 必须解析到 a3（一跳解析会返 null）", () => {
    const steps = [
      step("a", { status: "superseded", supersededBy: "a2" }),
      step("a2", { status: "superseded", supersededBy: "a3" }),
      step("a3"),
    ];
    const { byId, visible } = build(steps);
    expect(resolveDependency("a", byId, visible)).toBe("a3");
  });

  it("链断（旧步无替代者）→ null，不猜", () => {
    const steps = [step("a", { status: "superseded", supersededBy: null }), step("b")];
    const { byId, visible } = build(steps);
    expect(resolveDependency("a", byId, visible)).toBeNull();
  });

  it("replan 的哨兵串（supersededBy 不是步 id）→ null，不炸", () => {
    const steps = [step("a", { status: "superseded", supersededBy: "gen:2" }), step("b")];
    const { byId, visible } = build(steps);
    expect(resolveDependency("a", byId, visible)).toBeNull();
  });

  it("成环 → null 且不死循环", () => {
    const steps = [
      step("a", { status: "superseded", supersededBy: "b" }),
      step("b", { status: "superseded", supersededBy: "a" }),
    ];
    const { byId, visible } = build(steps);
    expect(resolveDependency("a", byId, visible)).toBeNull();
  });
});

describe("buildGraphModel", () => {
  it("扇入形状：首层并行 + 次层汇合，边指向替代步", () => {
    const steps = [
      step("s1", { status: "done", seq: 0 }),
      step("s2", { status: "superseded", supersededBy: "s2p", seq: 1 }),
      step("s2p", { status: "done", seq: 2, origin: "revision" }),
      step("s3", { level: 1, seq: 3, status: "running", dependsOn: ["s1", "s2"] }),
    ];
    const m = buildGraphModel(steps);
    expect(m.layers).toHaveLength(2);
    expect(m.layers[0].isParallel).toBe(true);
    expect(m.layers[0].steps.map((s) => s.id)).toEqual(["s1", "s2p"]);
    expect(m.layers[1].isFan).toBe(true);
    // s3 names old s2 in its dependencies → draw edge to s2p.
    expect(m.edges).toEqual([
      { from: "s1", to: "s3", state: "active" },
      { from: "s2p", to: "s3", state: "active" },
    ]);
    expect(m.joinCounts).toEqual([2]);
    expect(m.ghostBySuccessor.get("s2p")?.id).toBe("s2");
  });

  it("两条旧边解析到同一替代步 → 去重，不画重边", () => {
    const steps = [
      step("a", { status: "superseded", supersededBy: "c" }),
      step("b", { status: "superseded", supersededBy: "c" }),
      step("c", { status: "done" }),
      step("d", { level: 1, dependsOn: ["a", "b"], status: "done" }),
    ];
    const m = buildGraphModel(steps);
    expect(m.edges).toEqual([{ from: "c", to: "d", state: "done" }]);
  });

  it("上游 failed → 边断裂", () => {
    const steps = [
      step("a", { status: "failed" }),
      step("b", { level: 1, dependsOn: ["a"], status: "blocked" }),
    ];
    expect(buildGraphModel(steps).edges[0].state).toBe("broken");
  });

  it("纯平行 0 边：并行组本身是形状，不是空图", () => {
    const m = buildGraphModel([step("a"), step("b"), step("c")]);
    expect(m.edges).toHaveLength(0);
    expect(m.parallelCount).toBe(3);
    expect(m.layers[0].isParallel).toBe(true);
  });

  it("层内 >6 节点自动折 2 列（设计稿靠外部传参，这里按数量判定）", () => {
    const many = Array.from({ length: 7 }, (_, i) => step("s" + i, { seq: i }));
    expect(buildGraphModel(many).layers[0].isGrid).toBe(true);
    expect(buildGraphModel(many.slice(0, 6)).layers[0].isGrid).toBe(false);
  });

  it("层数 ≥3 → 需要横向滚动", () => {
    const steps = [step("a"), step("b", { level: 1 }), step("c", { level: 2 })];
    expect(buildGraphModel(steps).needsScroll).toBe(true);
    expect(buildGraphModel(steps.slice(0, 2)).needsScroll).toBe(false);
  });

  it("superseded 步不进画面", () => {
    const m = buildGraphModel([
      step("a", { status: "superseded", supersededBy: "b" }),
      step("b"),
      step("c"),
    ]);
    expect(m.layers.flatMap((l) => l.steps).map((s) => s.id)).toEqual(["b", "c"]);
  });
});

describe("shouldRenderGraph", () => {
  it("单步计划不渲染图区", () => {
    expect(shouldRenderGraph([step("a")])).toBe(false);
  });
  it("旧步不计入步数：修订过的单步计划仍不渲染", () => {
    expect(
      shouldRenderGraph([step("a", { status: "superseded", supersededBy: "a2" }), step("a2")]),
    ).toBe(false);
  });
  it("两步起渲染", () => {
    expect(shouldRenderGraph([step("a"), step("b")])).toBe(true);
  });
});

describe("verifyGlyphKey", () => {
  it("四态映射", () => {
    const mk = (verdict: "pass" | "partial" | "fail" | "skipped") =>
      step("x", { status: "done", verify: { verdict, unmet: [] } });
    expect(verifyGlyphKey(mk("pass"))).toBe("ok");
    expect(verifyGlyphKey(mk("partial"))).toBe("part");
    expect(verifyGlyphKey(mk("fail"))).toBe("unmet");
    expect(verifyGlyphKey(mk("skipped"))).toBe("none");
  });
  it("仅 done 步显示核验", () => {
    expect(
      verifyGlyphKey(step("x", { status: "running", verify: { verdict: "pass", unmet: [] } })),
    ).toBeNull();
  });
});

describe("isBlockedByUpstream", () => {
  const mk = (steps: PlanStepView[]) => ({
    byId: new Map(steps.map((s) => [s.id, s])),
    visible: new Set(steps.filter((s) => s.status !== "superseded").map((s) => s.id)),
  });

  it("上游 failed 拖累 → 给因果注记（不给操作）", () => {
    const steps = [
      step("a", { status: "failed" }),
      step("b", { level: 1, dependsOn: ["a"], status: "blocked" }),
    ];
    const { byId, visible } = mk(steps);
    expect(isBlockedByUpstream(steps[1], byId, visible)).toBe(true);
  });

  it("自身受阻(等用户补材料) → 不是上游拖累", () => {
    const steps = [
      step("a", { status: "done" }),
      step("b", { level: 1, dependsOn: ["a"], status: "blocked" }),
    ];
    const { byId, visible } = mk(steps);
    expect(isBlockedByUpstream(steps[1], byId, visible)).toBe(false);
  });
});

describe("needsHorizontalScroll", () => {
  // ★2026-08-13 device bug: layer-count threshold 3 never enables scrolling for 2-layer plans, while .gcanvas
  // defaults to overflow-x:visible, so the overflowing second layer is clipped by the card edge and **unreachable**.
  // User example: 6 steps (5 parallel → 1 summary); summary node「合规风险」was half clipped.
  it("2 层但装不下时也要开滚动（本次 bug 本体）", () => {
    expect(needsHorizontalScroll({ layerCount: 2, contentW: 900, availW: 802 })).toBe(true);
  });

  it("★正对照：2 层且装得下 → 不开，行为逐字不变", () => {
    // Observed 2-layer plan: 442+96+240=778, canvas 802, just fits.
    expect(needsHorizontalScroll({ layerCount: 2, contentW: 778, availW: 802 })).toBe(false);
  });

  it("层数 ≥3 仍恒开（原规格页判据保留为首屏兜底）", () => {
    expect(needsHorizontalScroll({ layerCount: 3, contentW: 0, availW: 0 })).toBe(true);
  });

  it("没量到几何时不猜（jsdom / 首帧 → 0）", () => {
    expect(needsHorizontalScroll({ layerCount: 2, contentW: 0, availW: 0 })).toBe(false);
    expect(needsHorizontalScroll({ layerCount: 1, contentW: 900, availW: 0 })).toBe(false);
  });

  it("1px 容差：亚像素舍入不该冒出滚动条", () => {
    expect(needsHorizontalScroll({ layerCount: 2, contentW: 802.6, availW: 802 })).toBe(false);
    expect(needsHorizontalScroll({ layerCount: 2, contentW: 804, availW: 802 })).toBe(true);
  });
});

describe("阶梯编号列的两个状态（设计稿 1c）", () => {
  const S = (status: string) => ({ id: "x" + status, status }) as never;

  it("圈：有在跑 → running；全部 done/skipped → done；否则 pending", () => {
    expect(stepperRingState([S("done"), S("running")])).toBe("running");
    expect(stepperRingState([S("done"), S("skipped")])).toBe("done");
    expect(stepperRingState([S("done"), S("pending")])).toBe("pending");
    expect(stepperRingState([])).toBe("pending");
  });

  it("★连线：broken 压过 done —— 三成功一失败时下游拿到的是残缺输入", () => {
    expect(stepperLinkState([S("done"), S("done"), S("done"), S("failed")])).toBe("broken");
    expect(stepperLinkState([S("done"), S("blocked")])).toBe("broken");
    // Positive control: without failures it must not always be broken; an always-red line carries no signal.
    expect(stepperLinkState([S("done"), S("done")])).toBe("done");
    expect(stepperLinkState([S("done"), S("running")])).toBe("running");
    expect(stepperLinkState([S("pending")])).toBe("pending");
  });

  it("skipped 算跑成了，不算断 —— 用户主动跳过不是故障", () => {
    expect(stepperLinkState([S("done"), S("skipped")])).toBe("done");
    expect(stepperRingState([S("skipped")])).toBe("done");
  });

  it("brokenCount 数的是 failed/blocked", () => {
    expect(brokenCount([S("failed"), S("blocked"), S("done"), S("skipped")])).toBe(2);
    expect(brokenCount([S("done")])).toBe(0);
  });
});

describe("编号圈：跑完了就不许再说「还没到」（116 真机 2026-08-14）", () => {
  const S = (status: string) => ({ id: "x" + status, status }) as never;

  it("★整层全失败 → 圈是「跑完」不是「没到」", () => {
    // Device observation: BI repeatedly failed, both steps in the layer failed, yet the ring stayed gray is-pending = not reached.
    // The left column is a progress bar by design; claiming not reached is false information.
    // Mutation: remove failed from _RING_SETTLED → this fails.
    expect(stepperRingState([S("failed"), S("failed")])).toBe("done");
  });

  it("混合层（成功+失败）→ 圈说「这层跑过了」，与那段注释一致", () => {
    expect(stepperRingState([S("done"), S("done"), S("failed")])).toBe("done");
  });

  it("失败信号仍由连线承载，不被圈盖掉", () => {
    // Ring=done and link=broken must hold together, precisely why they are deliberately separate.
    expect(stepperRingState([S("done"), S("failed")])).toBe("done");
    expect(stepperLinkState([S("done"), S("failed")])).toBe("broken");
  });

  it("负对照：blocked 不算跑完（它还没轮到）", () => {
    expect(stepperRingState([S("failed"), S("blocked")])).toBe("pending");
    expect(stepperRingState([S("blocked")])).toBe("pending");
  });

  it("负对照：还有没跑的步时仍是 pending", () => {
    expect(stepperRingState([S("failed"), S("pending")])).toBe("pending");
    expect(stepperRingState([S("failed"), S("running")])).toBe("running");
  });
});
