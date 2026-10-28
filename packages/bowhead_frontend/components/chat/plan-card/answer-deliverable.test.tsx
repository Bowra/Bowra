/**
 * Completed ANSWER plans must not show a report surface.
 *
 * plan.deliverable_kind == "answer" delivers a chat answer: completion leaves no report
 * on canvas, yet the card showed a「Report ready ✓」badge +「View report」button (opening an empty canvas).
 * Gate on deliverableKind; undefined (old events/backends) is treated as report, keeping behavior
 * byte-for-byte identical to before the fix.
 */
import { describe, it, expect, afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";
import { PlanCard } from "./PlanCard";
import type { PlanStepView, PlanView } from "./types";

afterEach(() => cleanup());

function step(id: string): PlanStepView {
  return {
    id,
    agent: "compute",
    objective: `目标 ${id}`,
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
  } as unknown as PlanStepView;
}

function render(deliverableKind?: string) {
  const plan = {
    planId: "p1",
    title: "计划",
    status: "completed",
    steps: [step("s1")],
    ...(deliverableKind ? { deliverableKind } : {}),
  } as unknown as PlanView;
  return renderIntl(<PlanCard plan={plan} />).container;
}

describe("ANSWER plan 完成态 — 无报告不显报告面", () => {
  it("deliverableKind=answer → 无「报告已生成」徽章、无「查看报告」按钮", () => {
    const c = render("answer");
    expect(c.textContent).not.toContain("报告已生成");
    // ⚠ Follow the **current** `report_generated` copy. On 2026-08-15, English changed from
    // "Report ready" (claims readiness without any acceptance check) to "Report generated"
    // (states a fact, equivalent to zh「报告已生成」); retaining the old string makes this always true,
    // turning it into a dead assertion that can no longer catch the badge returning.
    expect(c.textContent).not.toContain("Report generated");
    expect(c.querySelector(".pfoot .pbtn.pos")).toBeNull();
  });

  it("正对照 deliverableKind=report → 徽章与按钮照旧", () => {
    const c = render("report");
    expect(c.querySelector(".pc-modetag")).not.toBeNull();
    expect(c.querySelector(".pfoot .pbtn.pos")).not.toBeNull();
  });

  it("正对照 老事件缺字段 → 按 report 处理（行为不变）", () => {
    const c = render(undefined);
    expect(c.querySelector(".pfoot .pbtn.pos")).not.toBeNull();
  });
});
