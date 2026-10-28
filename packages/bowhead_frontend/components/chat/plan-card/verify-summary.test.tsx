/**
 * chat-ux-2026-08 dogfood fix — keep header verification outcomes separate.
 *
 * Observed on dev 1.0.77 (`dogfood-dev-1.0.77-2026-08-08.md` §3.5): all three steps were `done`,
 * verdicts were「○未核验 / ○未核验 / **✕未达标**」, but the header showed **「1 步已核验」**,
 * with an all-green completed card + `Report ready ✓`; `verifiedN > 0` also bypassed gray styling.
 *
 * The count combined `pass` / `partial` / **`fail`**:
 *     steps.filter(s => s.verify && s.verify.verdict !== "skipped").length
 * Only `skipped` was excluded, so **a plan failing every acceptance check showed「3 步已核验」**.
 *
 * ⚠ This was **a half-finished fix**, not a new problem: `VerifyBadge` comments explicitly said
 * fail and partial must stay distinct, and skipped and pass visually distinguishable — broken verifier
 * configuration must not look all-green. Per-step badges did differ, but header aggregation merged them again.
 *
 * Mutation guard: replace the three counts with one `verdict !== "skipped"`; the ★ tests must fail.
 */
import { describe, it, expect, afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";
import { PlanCard } from "./PlanCard";
import type { PlanStepView, PlanView } from "./types";

afterEach(() => cleanup());

type Verdict = "pass" | "partial" | "fail" | "skipped";

function step(id: string, seq: number, verdict?: Verdict): PlanStepView {
  return {
    id,
    agent: "compute",
    objective: `目标 ${id}`,
    level: 0,
    dependsOn: [],
    needsCorpus: false,
    status: "done",
    included: true,
    seq,
    deliverable: null,
    reason: null,
    errorKind: null,
    supersededBy: null,
    needsWeb: false,
    feasibility: null,
    ...(verdict ? { verify: { verdict, unmet: verdict === "fail" ? ["缺口径"] : [] } } : {}),
  } as unknown as PlanStepView;
}

function head(steps: PlanStepView[]): string {
  const { container } = renderIntl(
    <PlanCard
      plan={{ planId: "p1", title: "计划", status: "completed", steps } as unknown as PlanView}
    />,
  );
  return (container.querySelector(".pc-head") as HTMLElement).textContent ?? "";
}

describe("完成态头部 — 核验汇总按 verdict 分段，不合流", () => {
  it("★ 未达标不算已核验（116/dev 实测形态：2 未核验 + 1 未达标）", () => {
    const txt = head([step("a", 0, "skipped"), step("b", 1, "skipped"), step("c", 2, "fail")]);
    expect(txt).toContain("3 / 3 完成");
    // The old implementation showed「1 步已核验」here — precisely the one step that **failed acceptance**.
    expect(txt).not.toContain("1 步已核验");
    expect(txt).toContain("未核验"); // Zero passing steps → gray unverified copy.
    expect(txt).toContain("1 步未达标"); // Also state failed acceptance separately.
  });

  it("★ 全部未达标时**绝不**说「3 步已核验」", () => {
    const txt = head([step("a", 0, "fail"), step("b", 1, "fail"), step("c", 2, "fail")]);
    expect(txt).not.toContain("3 步已核验");
    expect(txt).toContain("3 步未达标");
  });

  it("★ 部分达标也不并进「已核验」", () => {
    const txt = head([step("a", 0, "pass"), step("b", 1, "partial")]);
    expect(txt).toContain("1 步已核验"); // Only the step that truly passed.
    expect(txt).toContain("1 步部分达标");
  });

  it("全 pass 时正常陈述（回归面：别把好路也改坏）", () => {
    const txt = head([step("a", 0, "pass"), step("b", 1, "pass")]);
    expect(txt).toContain("2 步已核验");
    expect(txt).not.toContain("未达标");
    expect(txt).not.toContain("部分达标");
  });

  it("verifier 全跳过仍不宣称已核验（批1 review A8 的既有裁决不能被本次改动破坏）", () => {
    const txt = head([step("a", 0, "skipped"), step("b", 1, "skipped")]);
    expect(txt).not.toContain("已核验");
    expect(txt).toContain("未核验");
  });
});

describe("完成态头部核验段的颜色（原内联样式改修饰类，工作台与画布第 1 批）", () => {
  function headEl(steps: PlanStepView[]): HTMLElement {
    const { container } = renderIntl(
      <PlanCard plan={{ planId: "p1", title: "计划", status: "completed", steps } as unknown as PlanView} />,
    );
    return container.querySelector(".pc-head") as HTMLElement;
  }

  it("没有已核验步时「未核验」用次要色；有已核验步时不挂修饰类", () => {
    const none = headEl([step("a", 1), step("b", 2)]);
    expect(none.querySelectorAll(".legacy-pc-verify--none")).toHaveLength(1);
    cleanup();
    const some = headEl([step("a", 1, "pass"), step("b", 2)]);
    expect(some.querySelectorAll(".legacy-pc-verify--none")).toHaveLength(0);
    // Icons carry their stroke inline (components/icon-refresh.tsx); the verify segments themselves have no inline style.
    expect(some.querySelectorAll("[style]:not(svg.bh-icon)")).toHaveLength(0);
  });

  it("部分达标与跳过的步用警示色，未达标用危险色", () => {
    const skippedStep = { ...step("c", 3), status: "skipped" } as PlanStepView;
    const head = headEl([step("a", 1, "partial"), step("b", 2, "fail"), skippedStep]);
    expect(head.querySelectorAll(".legacy-pc-verify--warn")).toHaveLength(2);
    const danger = head.querySelectorAll(".legacy-pc-verify--danger");
    expect(danger).toHaveLength(1);
    expect(danger[0].textContent).toMatch(/1/);
    expect(head.querySelectorAll("[style]:not(svg.bh-icon)")).toHaveLength(0);
  });
});
