import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, screen } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";
import { PlanCard } from "./PlanCard";
import type { PlanView } from "./types";

afterEach(() => cleanup());

function runningPlan(): PlanView {
  return {
    planId: "plan_1",
    title: "整理飞书反馈 bug",
    status: "running",
    steps: [
      {
        id: "s_done",
        agent: "kb",
        objective: "读取最新飞书文档",
        level: 0,
        dependsOn: [],
        needsCorpus: false,
        status: "done",
        included: true,
        seq: 0,
      },
      {
        id: "s_running",
        agent: "compute",
        objective: "定位每个反馈背后的源头实现和验证缺口",
        level: 1,
        dependsOn: ["s_done"],
        needsCorpus: false,
        status: "running",
        included: true,
        seq: 1,
        deliverable: "技术调研报告",
      },
      {
        id: "s_pending",
        agent: "writer",
        objective: "整理修复闭环",
        level: 2,
        dependsOn: ["s_running"],
        needsCorpus: false,
        status: "pending",
        included: true,
        seq: 2,
      },
    ],
  };
}

describe("PlanCard", () => {
  it("running 态仍看得到「当前在跑哪一步」; 飞书 #7 no 取消执行 button (停止 is single entry)", () => {
    // chat-ux-2026-08 plan integration batch: `RunningDetail` (「当前执行细节」block) was removed.
    // Since batch 5, **graph-node highlighting + list-row states** carry its information, previously repeated three times in the card
    // (real-browser evidence is in PlanCard.tsx's comment at that location).
    // ⚠ Thus **move assertion targets, do not delete the test**: users must still see which step is running,
    // preserving that user-facing capability on another surface. [[feedback_moved_capability_vs_dropped_capability]]
    const { container } = renderIntl(<PlanCard plan={runningPlan()} onCancel={vi.fn()} />);

    // The removed block must not reappear (otherwise removal was incomplete).
    expect(screen.queryByText("当前执行细节")).toBeNull();
    expect(container.querySelector(".prun-detail")).toBeNull();

    // The running step remains visible: objective/deliverable stay in list rows, with a running-state marker.
    expect(screen.getAllByText("定位每个反馈背后的源头实现和验证缺口").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("技术调研报告").length).toBeGreaterThanOrEqual(1);
    // List running marker = StatusPill's `.pp.run` (spinner + status_running copy).
    expect(container.querySelector(".pp.run")).toBeTruthy();
    expect(screen.getAllByText("进行中").length).toBeGreaterThanOrEqual(1);

    // Feishu 07.06 #7: running variant no longer has「取消执行」(duplicates composer Stop).
    expect(screen.queryByRole("button", { name: "取消执行" })).toBeNull();
  });
});

describe("graph-mode 批1: criteria + verify badge", () => {
  it("renders success criteria on steps and distinct badges for pass/partial/skipped", () => {
    const plan = runningPlan();
    plan.status = "completed";
    plan.steps = plan.steps.map((s, i) => ({
      ...s,
      status: "done" as const,
      successCriteria: ["含 2025 各季度数字"],
      verify:
        i === 0
          ? { verdict: "pass" as const, unmet: [] }
          : i === 1
            ? { verdict: "partial" as const, unmet: ["含 2025 各季度数字"] }
            : { verdict: "skipped" as const, unmet: [], note: "no_criteria" },
    }));
    renderIntl(<PlanCard plan={plan} />);
    // skipped and pass must be distinguishable (coverage signal, review A8).
    expect(screen.getAllByTestId("verify-pass").length).toBeGreaterThan(0);
    expect(screen.getAllByTestId("verify-partial").length).toBeGreaterThan(0);
    expect(screen.getAllByTestId("verify-skipped").length).toBeGreaterThan(0);
  });

  it("proposed variant lists criteria under each step", () => {
    const plan = runningPlan();
    plan.status = "proposed";
    plan.steps = plan.steps.map((s) => ({ ...s, status: "pending" as const, successCriteria: ["引用 ≥2 个官方信源"] }));
    renderIntl(<PlanCard plan={plan} />);
    expect(screen.getAllByTestId("step-criteria").length).toBe(plan.steps.length);
  });
});
