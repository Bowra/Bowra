/**
 * chat-ux-2026-08 plan integration batch — plan cards and execution disclosure share **one** agent naming scheme.
 *
 * Real-browser evidence (`/spike/plan-integration`, fed production shapes: `agent_spawned` →
 * `peerNames`/`peerDisplayNames`): within one turn, about 200px apart,
 * the same agent had two names because cards used the **independent** `plan_card.role_*` dictionary,
 * while chat outside used canonical `agentDisplayName`.
 *
 * Pin each of the old dictionary's three problems:
 *   ① **Name collisions**: `LABEL_KEY` mapped compute and business to `role_data`,
 *      giving compute and business-analyst identical card names;
 *   ② Covered only 5/10 agents, leaving raw English slugs for the others;
 *   ③ Ignored **team-configured** subagent names (built-in subagent admin sprint capability).
 *
 * Mutation guard: revert `planAgentLabel` to `role.labelKey ? t(role.labelKey) : agent`;
 * all ①②③ groups must fail.
 */
import { describe, it, expect, afterEach } from "vitest";
import { cleanup, screen } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";
import { agentDisplayName } from "../step-list/agent-meta";
import { _resetAllPlansForTesting, applyPlanProposed, applyPlanSnapshot, getPlanSnapshot } from "@/lib/plan-store";

import { PlanCard } from "./PlanCard";
import { planAgentLabel } from "./roles";
import type { PlanStepView, PlanView } from "./types";

afterEach(() => cleanup());

function step(id: string, agent: string, seq: number): PlanStepView {
  return {
    id,
    agent,
    objective: `目标 ${id}`,
    level: 0,
    dependsOn: [],
    needsCorpus: false,
    status: "running",
    included: true,
    seq,
    deliverable: null,
    reason: null,
    errorKind: null,
    supersededBy: null,
    needsWeb: false,
    feasibility: null,
  } as unknown as PlanStepView;
}

function plan(steps: PlanStepView[]): PlanView {
  return { planId: "p1", title: "计划", status: "running", steps } as unknown as PlanView;
}

describe("planAgentLabel — 与执行披露同源", () => {
  it("★ 就是 agentDisplayName（不是另一套词表）", () => {
    for (const a of ["compute", "financial-analyst", "business-analyst", "kb-analyst", "canvas-writer", "lead"]) {
      expect(planAgentLabel(a)).toBe(agentDisplayName(a));
    }
  });

  it("★ 团队**配置的**名字优先（旧词表完全无视它）", () => {
    expect(planAgentLabel("financial-analyst", "王会计")).toBe("王会计");
    // Empty/whitespace configuration is not a configured name; fall back to canonical name, never an empty string.
    expect(planAgentLabel("financial-analyst", "   ")).toBe(agentDisplayName("financial-analyst"));
    expect(planAgentLabel("financial-analyst", null)).toBe(agentDisplayName("financial-analyst"));
  });
});

describe("计划卡渲染 — 三个旧毛病都不再复现", () => {
  it("★ compute 与 business-analyst **不再同名**（旧词表双双映到 role_data）", () => {
    // ⚠ This pair must be `compute` and `business-analyst`. Initially I used `data-analyst`
    // and `business-analyst`, making **a vacuous assertion**: `data-analyst` was absent from
    // agent-meta's old alias table (including built-in financial/business/compliance/compute
    // names), so generic fallback returned its original name,
    // already different from business-analyst **in old code**, guarding nothing.
    // Actual old LABEL_KEY collision was compute→role_data and business→role_data.
    const { container } = renderIntl(
      <PlanCard plan={plan([step("a", "compute", 0), step("b", "business-analyst", 1)])} />,
    );
    const names = Array.from(container.querySelectorAll(".pstep-name, .gn-name"))
      .map((n) => (n.textContent || "").trim())
      .filter(Boolean);
    const a = planAgentLabel("compute");
    const b = planAgentLabel("business-analyst");
    expect(a).not.toBe(b);
    expect(names).toContain(a);
    expect(names).toContain(b);
  });

  it("★ 旧词表没覆盖的 agent 不再显示原始英文 slug", () => {
    const { container } = renderIntl(
      <PlanCard plan={plan([step("a", "consultant", 0), step("b", "marketing", 1)])} />,
    );
    const text = container.textContent || "";
    expect(text).not.toContain("consultant");
    expect(text).not.toContain("marketing");
    expect(text).toContain(planAgentLabel("consultant"));
    expect(text).toContain(planAgentLabel("marketing"));
  });

  it("★ 配置名在**列表行和图节点两处都**渲染（join key = sub_<planStepId>）", () => {
    // ⚠ Assert **each surface separately**. Initially checking only whether「王会计」appeared anywhere let removing the list's
    // displayNameFor pass mutation tests because the graph node also showed that name; one surface satisfied
    // the assertion. This batch requires both surfaces to use the same name, so pin both.
    const { container } = renderIntl(
      <PlanCard
        plan={plan([step("ps_x", "financial-analyst", 0), step("ps_y", "compute", 1)])}
        displayNameFor={(id) => (id === "ps_x" ? "王会计" : undefined)}
      />,
    );
    const textsOf = (sel: string) =>
      Array.from(container.querySelectorAll(sel)).map((n) => (n.textContent || "").trim());

    expect(textsOf(".pstep-name")).toContain("王会计"); // List row.
    expect(textsOf(".gn-name")).toContain("王会计"); // Graph node.

    // Unconfigured agent falls back to canonical name, unaffected by the prior one (check both surfaces too).
    const fallback = planAgentLabel("compute");
    expect(textsOf(".pstep-name")).toContain(fallback);
    expect(textsOf(".gn-name")).toContain(fallback);
    expect(screen.queryAllByText("financial-analyst").length).toBe(0);
  });
});

describe("内部智能体 id 不外露（设计稿截图：汇总步显示 canvas-writer）", () => {
  afterEach(() => {
    document.documentElement.lang = "";
    _resetAllPlansForTesting();
  });

  const textsOf = (container: HTMLElement, sel: string) =>
    Array.from(container.querySelectorAll(sel)).map((n) => (n.textContent || "").trim());

  it("★ 已退役的 canvas-writer（旧计划 / 重放）显示「报告编辑」，列表与图两处都是", () => {
    const { container } = renderIntl(
      <PlanCard plan={plan([step("a", "kb-analyst", 0), step("b", "canvas-writer", 1)])} />,
    );
    expect(textsOf(container, ".pstep-name")).toContain("报告编辑");
    expect(textsOf(container, ".gn-name")).toContain("报告编辑");
    expect(container.textContent).not.toContain("canvas-writer");
  });

  it("★ 英文界面显示 Canvas Editor", () => {
    document.documentElement.lang = "en-US";
    const { container } = renderIntl(
      <PlanCard plan={plan([step("a", "canvas-writer", 0)])} />,
      { locale: "en-US" },
    );
    expect(textsOf(container, ".pstep-name")).toContain("Canvas Editor");
    expect(container.textContent).not.toContain("canvas-writer");
  });

  it("★ 其他系统保留名（非现役内置）回落中性「专家 / Specialist」", () => {
    for (const name of ["modeler", "analyst", "echo", "meta-agent", "narrative", "composer", "debate"]) {
      document.documentElement.lang = "zh-CN";
      expect(agentDisplayName(name)).toBe("专家");
      document.documentElement.lang = "en-US";
      expect(agentDisplayName(name)).toBe("Specialist");
    }
  });

  it("团队自建子代理没有配置名时仍显示它自己的名字（那是团队起的名，不是内部 id）", () => {
    expect(agentDisplayName("market-researcher")).toBe("market-researcher");
  });

  it("★ live（plan_proposed）与 F5 重放（snapshot）渲染同一个名字", () => {
    const rawSteps = [
      { id: "s1", agent: "kb-analyst", objective: "检索", level: 0, depends_on: [], needs_corpus: true },
      { id: "s2", agent: "canvas-writer", objective: "汇总", level: 1, depends_on: ["s1"], needs_corpus: false },
    ];
    applyPlanProposed({ plan_id: "p_live", title: "计划", steps: rawSteps });
    const live = renderIntl(<PlanCard plan={getPlanSnapshot("p_live")!} />);
    const liveNames = textsOf(live.container, ".pstep-name");
    cleanup();

    applyPlanSnapshot({
      plan_id: "p_replay",
      plan: { id: "p_replay", status: "proposed", title: "计划" },
      steps: rawSteps.map((s) => ({ ...s, status: "pending" })),
    });
    const replay = renderIntl(<PlanCard plan={getPlanSnapshot("p_replay")!} />);
    const replayNames = textsOf(replay.container, ".pstep-name");

    expect(liveNames).toContain("报告编辑");
    expect(replayNames).toEqual(liveNames);
  });
});

describe("RunningDetail 已删除", () => {
  it("★ 「当前执行细节」块不再渲染（状态由图+列表承载，明细在卡外披露）", () => {
    const { container } = renderIntl(
      <PlanCard plan={plan([step("a", "compute", 0), step("b", "business-analyst", 1)])} />,
    );
    expect(container.querySelector(".prun-detail")).toBeNull();
    expect(container.querySelector(".prun-item")).toBeNull();
  });
});
