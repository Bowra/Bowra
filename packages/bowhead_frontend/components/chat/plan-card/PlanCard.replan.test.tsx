/**
 * Batch 3 S5: F-D1 closure (failed-step recovery) + superseded filtering + revise/replan preview panels.
 * F-D1 regression guard: `s.status==="failed"` must show recovery buttons (both previous gates recognized only blocked,
 * and actual dogfood hit failed steps lacking retry buttons).
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";
import { PlanCard, type PlanRecoveryHandlers } from "./PlanCard";
import type { PlanView } from "./types";

afterEach(() => cleanup());

function blockedPlanWithFailedStep(): PlanView {
  return {
    planId: "plan_1",
    title: "尽调",
    status: "blocked",
    steps: [
      {
        id: "s_done",
        agent: "kb",
        objective: "读文档",
        level: 0,
        dependsOn: [],
        needsCorpus: false,
        status: "done",
        included: true,
        seq: 0,
      },
      {
        id: "s_failed",
        agent: "compute",
        objective: "联网检索",
        level: 0,
        dependsOn: [],
        needsCorpus: false,
        status: "failed",
        errorKind: "TimeoutError",
        reason: "boom",
        included: true,
        seq: 1,
      },
      {
        id: "s_dead",
        agent: "compute",
        objective: "被修订替代的旧步",
        level: 0,
        dependsOn: [],
        needsCorpus: false,
        status: "superseded",
        supersededBy: "s_rev",
        included: true,
        seq: 2,
      },
      {
        id: "s_rev",
        agent: "consultant",
        objective: "修订后的新步",
        level: 0,
        dependsOn: [],
        needsCorpus: false,
        status: "pending",
        origin: "revision",
        included: true,
        seq: 3,
      },
    ],
  };
}

function recovery(overrides: Partial<PlanRecoveryHandlers> = {}): PlanRecoveryHandlers {
  return {
    onRetry: vi.fn(),
    onSkip: vi.fn(),
    onSupply: vi.fn(),
    onRevise: vi.fn(),
    ...overrides,
  };
}

describe("PlanCard 批3", () => {
  it("F-D1 回归钉: failed 步渲染恢复动作(修订重试/继续执行/跳过)", () => {
    const rec = recovery();
    const { container } = renderIntl(
      <PlanCard plan={blockedPlanWithFailedStep()} recovery={rec} />,
    );
    // Batch 5: graph also links directly to recovery with identical copy; narrow assertions to the **list-side** recovery bar,
    // pinning that graphs do not reduce list functionality (mandate ⑤-1), never allowing either surface to satisfy it.
    const bar = container.querySelector(".pblock-acts") as HTMLElement;
    expect(bar).toBeTruthy();
    const text = (label: string) =>
      [...bar.querySelectorAll("button")].find((b) => b.textContent?.includes(label));
    expect(text("修订重试")).toBeTruthy();
    expect(text("继续执行")).toBeTruthy();
    expect(text("跳过该步")).toBeTruthy();
    // failed prefix rather than「受阻」.
    expect(screen.getByText("执行失败：")).toBeTruthy();
    fireEvent.click(text("修订重试")!);
    expect(rec.onRevise).toHaveBeenCalledWith("s_failed");
  });

  it("superseded 死行滤除渲染; 替代步上「已修订」章; 进度分母收窄", () => {
    const { container } = renderIntl(
      <PlanCard plan={blockedPlanWithFailedStep()} recovery={recovery()} />,
    );
    expect(screen.queryByText("被修订替代的旧步")).toBeNull();
    // Likewise, full objective must appear in the list row (graph detail card does not count).
    expect(
      (container.querySelector('.pstep[data-row="s_rev"]') as HTMLElement).textContent,
    ).toContain("修订后的新步");
    // Batch 5: graph nodes also show「已修订」badges; assert **inside the list row** (implementation review P2-D:
    // relaxing to getAllByText(...).length>0 kept passing after removing list badges, providing no guard).
    const revisedRow = container.querySelector('.pstep[data-row="s_rev"]') as HTMLElement;
    expect(revisedRow).toBeTruthy();
    expect(revisedRow.textContent).toContain("已修订");
    // Denominator 3 (=4-1 dead row): "1 / 3 完成".
    expect(screen.getByText(/1 \/ 3/)).toBeTruthy();
  });

  it("meta 计数把 failed 算进「需你处理」(不显示 0 步受阻)", () => {
    renderIntl(<PlanCard plan={blockedPlanWithFailedStep()} recovery={recovery()} />);
    expect(screen.getByText(/1 步受阻/)).toBeTruthy();
  });

  it("revise 预览面板: 展示修订产物+确认/放弃", () => {
    const onConfirm = vi.fn();
    const onDiscard = vi.fn();
    renderIntl(
      <PlanCard
        plan={blockedPlanWithFailedStep()}
        recovery={recovery()}
        revise={{
          stepId: "s_failed",
          agent: "consultant",
          objective: "改用顾问路线",
          deliverable: "",
          criteria: ["含 2025 数值"],
          warning: "该步已修订 2 次仍失败",
          onConfirm,
          onDiscard,
        }}
      />,
    );
    expect(screen.getByText("修订预览")).toBeTruthy();
    expect(screen.getByText("改用顾问路线")).toBeTruthy();
    expect(screen.getByText("含 2025 数值")).toBeTruthy();
    expect(screen.getByText(/已修订 2 次/)).toBeTruthy();
    fireEvent.click(screen.getByText("确认并重跑"));
    expect(onConfirm).toHaveBeenCalled();
  });

  it("replan 入口: 点「重新规划」进入 NL 输入态", () => {
    const onStart = vi.fn();
    renderIntl(<PlanCard plan={blockedPlanWithFailedStep()} recovery={recovery()} replan={{ onStart }} />);
    fireEvent.click(screen.getByText("重新规划"));
    expect(document.querySelector(".pnl input")).toBeTruthy();
  });

  // 116-dogfood: input shown after「重新规划」previously **reused amend's placeholder**,
  // without indicating replan mode; even with source in hand I first mistook it for normal「编辑计划」input,
  // thinking the button was broken (it only calls setReplanInput(true); submitting this input sends the request).
  it("replan 输入框用专属文案，不冒充「编辑计划」", () => {
    renderIntl(<PlanCard plan={blockedPlanWithFailedStep()} recovery={recovery()} replan={{ onStart: vi.fn() }} />);
    fireEvent.click(screen.getByText("重新规划"));
    const input = document.querySelector(".pnl input") as HTMLInputElement;
    expect(input).toBeTruthy();
    expect(input.placeholder).toContain("重排");
    // Negative guard: it must not use amend's sentence again.
    expect(input.placeholder).not.toContain("环境合规");
  });

  it("replan 预览: diff 摘要+确认", () => {
    const onConfirm = vi.fn();
    renderIntl(
      <PlanCard
        plan={blockedPlanWithFailedStep()}
        recovery={recovery()}
        replan={{
          onStart: vi.fn(),
          preview: {
            newSteps: [{ id: "ps_n1", agent: "consultant", objective: "新路线", criteria: [] }],
            keptDone: 1,
            superseded: 2,
            onConfirm,
            onDiscard: vi.fn(),
          },
        }}
      />,
    );
    expect(screen.getByText("重新规划预览")).toBeTruthy();
    expect(screen.getByText(/保留 1 个已完成步/)).toBeTruthy();
    expect(screen.getByText("新路线")).toBeTruthy();
    fireEvent.click(screen.getByText("确认并重跑"));
    expect(onConfirm).toHaveBeenCalled();
  });
});

describe("enforce 翻档前置（tech-design §5）", () => {
  function planWith(steps: PlanView["steps"], status: PlanView["status"] = "completed"): PlanView {
    return { planId: "plan_e", title: "尽调", status, steps };
  }
  const base: Omit<PlanView["steps"][number], "id" | "status"> = {
    agent: "compute",
    objective: "分析",
    level: 0,
    dependsOn: [],
    needsCorpus: false,
    included: true,
  };

  it("★②验收未达呈现「验收未达 + 未满足项」，不是系统错误面孔", () => {
    const { container } = renderIntl(
      <PlanCard
        plan={planWith(
          [
            { ...base, id: "s1", status: "failed", errorKind: "criteria_unmet", verify: { verdict: "fail", unmet: ["缺少同业口径", "未标注来源"] } },
            { ...base, id: "s2", seq: 1, status: "done" },
          ],
          "blocked",
        )}
        recovery={{ onRetry: vi.fn(), onSkip: vi.fn(), onSupply: vi.fn(), onRevise: vi.fn() }}
      />,
    );
    const bar = container.querySelector(".pblock") as HTMLElement;
    expect(bar.textContent).toContain("验收未达：");
    expect(bar.textContent).not.toContain("执行失败：");
    // Each unmet item remains visible (after enforce, users decide whether to change criteria or revise/retry).
    expect(bar.textContent).toContain("缺少同业口径");
    expect(bar.textContent).toContain("未标注来源");
    // Raw error_kind line is noise (it is simply criteria_unmet itself).
    expect(bar.textContent).not.toContain("criteria_unmet");
  });

  it("★③verifier 全跳过时头部不得宣称已核验", () => {
    const { container } = renderIntl(
      <PlanCard
        plan={planWith([
          { ...base, id: "s1", status: "done", verify: { verdict: "skipped", unmet: [] } },
          { ...base, id: "s2", seq: 1, status: "done", verify: { verdict: "skipped", unmet: [] } },
        ])}
      />,
    );
    const head = container.querySelector(".pc-head") as HTMLElement;
    expect(head.textContent).toContain("未核验");
    expect(head.textContent).not.toContain("已核验");
  });

  it("★③有真实核验时按真实覆盖率陈述（不是拿完成数冒充）", () => {
    const { container } = renderIntl(
      <PlanCard
        plan={planWith([
          { ...base, id: "s1", status: "done", verify: { verdict: "pass", unmet: [] } },
          { ...base, id: "s2", seq: 1, status: "done", verify: { verdict: "skipped", unmet: [] } },
          { ...base, id: "s3", seq: 2, status: "done" },
        ])}
      />,
    );
    const head = container.querySelector(".pc-head") as HTMLElement;
    expect(head.textContent).toContain("3 / 3 完成");
    expect(head.textContent).toContain("1 步已核验"); // Only s1 was actually verified.
  });
});
