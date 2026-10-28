/**
 * 116 dogfood 2026-08-15 — ✕ unmet criteria must say which criterion and why.
 *
 * Observed shape (plan_388aa20aee9e): **all three** acceptance criteria are unmet, yet users see
 * three identical dots + a ✕ badge, with `verify.note` an empty string ⇒ neither the unmet criterion
 * nor the reason is explained.
 *
 * The evidence **was always there**: `plan_verify`'s prompt expressly requires a single sentence in note
 * (≤40 characters) explaining what is missing when unmet; parsing retains it (truncated to 80 characters) and `to_json` persists it—it was only
 * discarded at the `routes/plans.py` API boundary, which returned just criterion text. Data exists but is withheld.
 *
 * This locks the contract: with `items`, the badge title must be criterion — evidence;
 * without them (old events/responses), fall back to criterion text alone, without crashing or becoming empty.
 */
import { describe, it, expect, afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";
import { PlanCard } from "./PlanCard";
import type { PlanStepView, PlanView } from "./types";
import { dispatchToTurnStore } from "@/lib/turn-store-dispatch";
import { _resetAllPlansForTesting, applyPlanSnapshot, applyPlanProposed, getPlanSnapshot } from "@/lib/plan-store";

afterEach(() => cleanup());

function step(verify: unknown): PlanStepView {
  return {
    id: "s1",
    agent: "compute",
    objective: "目标",
    level: 0,
    dependsOn: [],
    needsCorpus: false,
    status: "done",
    included: true,
    seq: 0,
    deliverable: null,
    reason: null,
    errorKind: null,
    supersededBy: null,
    needsWeb: false,
    feasibility: null,
    verify,
  } as unknown as PlanStepView;
}

function badgeTitle(verify: unknown): string {
  const { container } = renderIntl(
    <PlanCard
      plan={
        { planId: "p1", title: "计划", status: "completed", steps: [step(verify)] } as unknown as PlanView
      }
    />,
  );
  return container.querySelector('[data-testid="verify-fail"]')?.getAttribute("title") ?? "";
}

describe("✕未达标徽标 — 说得出哪一条、为什么", () => {
  it("★ 有 items 时 title = 「判据 —— 依据」，逐条都在", () => {
    const title = badgeTitle({
      verdict: "fail",
      unmet: ["含各月同比变化率数值", "含各月环比变化率数值"],
      note: "",
      items: [
        { text: "含2025年1-12月各月营业额数值", status: "met", note: "" },
        { text: "含各月同比变化率数值", status: "unmet", note: "只给了同比绝对值，没有变化率" },
        { text: "含各月环比变化率数值", status: "unmet", note: "全文没有环比数据" },
      ],
    });
    expect(title).toContain("含各月同比变化率数值 —— 只给了同比绝对值，没有变化率");
    expect(title).toContain("含各月环比变化率数值 —— 全文没有环比数据");
    // Exclude the met item—the title explains unmet criteria, rather than listing every criterion.
    expect(title).not.toContain("含2025年1-12月各月营业额数值");
  });

  it("★ 某条 unmet 但没写 note → 只出判据原文，不出「—— undefined」这种残串", () => {
    const title = badgeTitle({
      verdict: "fail",
      unmet: ["含环比"],
      note: "",
      items: [{ text: "含环比", status: "unmet" }],
    });
    expect(title).toBe("含环比");
    expect(title).not.toContain("undefined");
    expect(title).not.toContain("——");
  });

  it("★ 老响应没有 items → 退回只列判据原文（不许变空）", () => {
    // Positive backward-compatibility control: the pre-fix shape must remain usable.
    const title = badgeTitle({ verdict: "fail", unmet: ["含环比", "含同比"], note: "" });
    expect(title).toBe("含环比；含同比");
  });
});


afterEach(_resetAllPlansForTesting);

it.each(["live", "history"])("preserves uncertain evidence through %s state and rendering", (source) => {
  const verify = {
    verdict: "partial", unmet: [], note: "",
    items: [{ text: "Verify the refund definition", status: "cannot_tell", note: "No reply evidence was provided." }],
  };
  const wireStep = { id: "s1", agent: "compute", objective: "Calculate revenue", status: "done" };
  if (source === "live") {
    applyPlanProposed({ plan_id: "p1", title: "Revenue", steps: [wireStep] });
    expect(dispatchToTurnStore("step_done", { plan_id: "p1", step_id: "s1", verify })).toBe(true);
  } else {
    applyPlanSnapshot({ plan_id: "p1", plan: { title: "Revenue", status: "completed" }, steps: [{ ...wireStep, verify }] });
  }
  const plan = getPlanSnapshot("p1")!;
  expect(plan.steps[0].verify?.items).toEqual(verify.items);
  const { container } = renderIntl(<PlanCard plan={plan} />);
  const title = container.querySelector('[data-testid="verify-partial"]')?.getAttribute("title");
  expect(title).toContain("Verify the refund definition");
  expect(title).toContain("No reply evidence was provided.");
});
