/**
 * Phase 1.7-B — StepSummary component test.
 *
 * Pins the rendering + click contract per v8 `variant-a.jsx:204-230`:
 *   · Renders "{N} 步完成" with the steps' length
 *   · Renders the total duration when steps carry durationText
 *   · Renders "展开运行过程" call-to-action
 *   · Click fires onExpand
 *   · Single-step case still renders correctly
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";
import { StepSummary } from "./StepSummary";
import type { StepView } from "./types";

afterEach(() => cleanup());

function step(over: Partial<StepView>): StepView {
  return {
    stepId: "s1",
    parentStepId: null,
    concurrencyGroupId: null,
    stepType: "tool_run",
    toolName: null,
    headerZh: "执行步骤",
    state: "done",
    ...over,
  };
}

describe("StepSummary", () => {
  it("renders '{N} 步完成' with steps count", () => {
    const steps = [step({ stepId: "a", durationText: "0.4s" }), step({ stepId: "b", durationText: "1.0s" })];
    renderIntl(<StepSummary steps={steps} onExpand={() => {}} />);
    expect(screen.getByText(/2 步完成/)).toBeTruthy();
  });

  it("renders derived total duration when steps carry durationText", () => {
    const steps = [
      step({ stepId: "a", durationText: "0.4s" }),
      step({ stepId: "b", durationText: "1.0s" }),
    ];
    renderIntl(<StepSummary steps={steps} onExpand={() => {}} />);
    // 0.4s + 1.0s = 1400ms → use the band's formatElapsed, rounded to whole seconds.
    expect(screen.getByText(/· 1 秒/)).toBeTruthy();
  });

  // chat-ux-2026-08 — The collapsed strip must show **wall-clock** duration, not the sum of step durations.
  // Observed on 116: a plan turn actually ran for about 2 minutes but showed `✓ 10 步完成 · 1 次重试 · 15s`
  // (thinking gaps belong to no step). The fixture deliberately differs by an order of magnitude;
  // an equal-duration fixture would pass this assertion on either path.
  it("★ 墙钟 totalDurationMs 优先于各步耗时求和", () => {
    const steps = [
      step({ stepId: "a", durationText: "3.0s" }),
      step({ stepId: "b", durationText: "2.0s" }),
    ];
    renderIntl(
      <StepSummary steps={steps} totalDurationMs={124_000} onExpand={() => {}} />,
    );
    expect(screen.getByText(/· 2 分 4 秒/)).toBeTruthy();
    expect(screen.queryByText(/· 5 秒/)).toBeNull();
  });

  // The collapsed strip in `claude-design-prompt.md:226` reads `16 步完成 · 4 分 30 秒`;
  // the band uses the same formatter. Pin both surfaces rendering the same string for the same ms,
  // so nobody introduces a separate `Math.round(sec)s` formatter here.
  it("★ 时长文案与运行带同源（同一 ms → 同一串）", () => {
    renderIntl(<StepSummary steps={[]} totalDurationMs={270_000} onExpand={() => {}} />);
    expect(screen.getByText(/· 4 分 30 秒/)).toBeTruthy();
    expect(screen.queryByText(/270s/)).toBeNull();
  });

  // Render en-US with the real catalog: this duration path must contain no Chinese characters.
  // `RunningBand.tsx:86` records this bug: the previous formatElapsed hardcoded "秒 / 分",
  // rendering `3 分 25 秒` in en-US, the site's sole user-visible copy bypassing next-intl.
  // The collapsed strip now shares that formatter, so duplicate this guard here.
  it("★ en-US: 时长走 i18n，零中文", () => {
    const { container } = renderIntl(
      <StepSummary steps={[]} totalDurationMs={270_000} onExpand={() => {}} />,
      { locale: "en-US" },
    );
    expect(screen.getByText(/· 4 min 30 s/)).toBeTruthy();
    // Scan the whole collapsed strip for CJK: checking only the duration cell misses other hardcoding.
    expect(container.textContent ?? "").not.toMatch(/[一-鿿]/);
  });

  it("en-US: 不足 1 分钟只出秒", () => {
    renderIntl(
      <StepSummary steps={[]} totalDurationMs={36_000} onExpand={() => {}} />,
      { locale: "en-US" },
    );
    expect(screen.getByText(/· 36 s/)).toBeTruthy();
  });

  // jsdom cannot measure layout, so pin **source structure** (see docs/单元测试规范.md):
  // Action copy and arrow must share one flex item; the arrow **must not** be a direct button child.
  // Real-browser measurements (116 @1440, column 395px, scan 340–470 in 5px steps): separate items make English
  // `16 steps done · 2 retried · 12m 34s · Expand trace` at 450–460px
  // push the arrow alone onto a second line; wrapping them yields zero lone arrows over the same range.
  it("★ 动作文案与箭头是同一个 flex item（换行时不许拆开）", () => {
    renderIntl(<StepSummary steps={[]} totalDurationMs={270_000} onExpand={() => {}} />);
    const summary = screen.getByTestId("step-summary");
    const cta = summary.querySelector(".ui-card__trailing-label");
    expect(cta).toBeTruthy();
    // Text and arrow are inside one trailing item.
    expect(cta!.textContent).toBe("展开运行过程");
    expect(cta!.querySelector("svg")).toBeTruthy();
    // The arrow is no longer a direct button child (otherwise it can wrap independently).
    expect(summary.querySelector(":scope > svg")).toBeNull();
  });

  it("拿不到墙钟时才退回求和（兜底仍在）", () => {
    const steps = [
      step({ stepId: "a", durationText: "3.0s" }),
      step({ stepId: "b", durationText: "2.0s" }),
    ];
    renderIntl(<StepSummary steps={steps} totalDurationMs={null} onExpand={() => {}} />);
    expect(screen.getByText(/· 5 秒/)).toBeTruthy();
  });

  // The backend config-failure branch hardcodes `duration_ms: 0`; showing `· 0ms` is more misleading than omitting it.
  it("墙钟为 0（后端没量出来）时不当真，退回求和", () => {
    const steps = [step({ stepId: "a", durationText: "0.4s" })];
    renderIntl(<StepSummary steps={steps} totalDurationMs={0} onExpand={() => {}} />);
    expect(screen.getByText(/· 不到 1 秒/)).toBeTruthy();
  });

  it("renders '展开运行过程' call-to-action", () => {
    const steps = [step({ stepId: "a" })];
    renderIntl(<StepSummary steps={steps} onExpand={() => {}} />);
    expect(screen.getByText(/展开运行过程/)).toBeTruthy();
  });

  it("fires onExpand when clicked", () => {
    const onExpand = vi.fn();
    const steps = [step({ stepId: "a" })];
    renderIntl(<StepSummary steps={steps} onExpand={onExpand} />);
    fireEvent.click(screen.getByTestId("step-summary"));
    expect(onExpand).toHaveBeenCalledTimes(1);
  });

  it("is one row card button with a muted check, no inline root style", () => {
    const steps = [step({ stepId: "a", durationText: "0.4s" })];
    renderIntl(<StepSummary steps={steps} onExpand={() => {}} />);

    const summary = screen.getByTestId("step-summary");
    expect(summary.tagName).toBe("BUTTON");
    expect(summary.className).toContain("ui-card--row");
    expect(summary.className).toContain("ui-card--icon-muted");
    expect(summary.getAttribute("style")).toBeNull();
    expect(summary.querySelector(".ui-card__icon svg")?.getAttribute("class")).toContain("lucide-circle-check");
    expect(summary.querySelector(".ui-card__line")?.textContent).toContain("1 步完成");
    expect(summary.querySelector('[data-testid="step-summary-duration"]')?.textContent).toContain("不到 1 秒");
    expect(summary.querySelector(".ui-card__trailing-label")?.textContent).toContain("展开运行过程");
  });

  it("renders without duration when no step carries one", () => {
    const steps = [step({ stepId: "a" }), step({ stepId: "b" })];
    renderIntl(<StepSummary steps={steps} onExpand={() => {}} />);
    // Should NOT have any "· X.Xs" text since no durations.
    const summary = screen.getByTestId("step-summary");
    expect(summary.textContent).toContain("2 步完成");
    expect(summary.textContent).not.toMatch(/· \d+(\.\d+)?s/);
  });

  it("renders single-step case correctly", () => {
    const steps = [step({ stepId: "a", durationText: "0.4s" })];
    renderIntl(<StepSummary steps={steps} onExpand={() => {}} />);
    expect(screen.getByText(/1 步完成/)).toBeTruthy();
  });

  it("does not claim zero steps for a peer-only execution", () => {
    renderIntl(
      <StepSummary
        steps={[]}
        peerNames={{ peer_1: "financial-analyst" }}
        onExpand={() => {}}
      />,
    );

    const summary = screen.getByTestId("step-summary");
    expect(summary.textContent).toContain("执行完成");
    expect(summary.textContent).not.toContain("0 步完成");
    // Agent colour badges are replaced by the Agent count (spec §8).
    expect(summary.querySelectorAll(".ag-badge")).toHaveLength(0);
    expect(summary.textContent).toContain("调用 1 个智能体");
  });

  it("counts all 10 configured Agents without a +N collapse", () => {
    const peerNames = Object.fromEntries(
      Array.from({ length: 10 }, (_, index) => [`peer_${index}`, `agent-${index}`]),
    );
    const steps = [
      step({ stepId: "lead", subagentId: "LEAD" }),
      ...Array.from({ length: 10 }, (_, index) =>
        step({
          stepId: `spawn_${index}`,
          stepType: "subagent",
          toolName: "subagent_spawn",
          subagentId: "LEAD",
          peerProseKey: `peer_${index}`,
        }),
      ),
    ];

    renderIntl(
      <StepSummary
        steps={steps}
        peerNames={peerNames}
        onExpand={() => {}}
      />,
    );

    const summary = screen.getByTestId("step-summary");
    expect(summary.querySelectorAll(".ag-badge")).toHaveLength(0);
    expect(summary.textContent).toContain("11 步完成 · 调用 10 个智能体");
    expect(summary.textContent).not.toMatch(/\+\d+/);
  });

  describe("step outcome (chat turn feedback D6)", () => {
    const issues = [step({ stepId: "a", state: "failed" }), step({ stepId: "b", state: "partial" }), step({ stepId: "c" })];
    const outcome = (over: Partial<{ issueSteps: number; recoveredSteps: number; unresolvedSteps: number; answerAffected: boolean; turnCompleted: boolean }>) => ({
      issueSteps: 2,
      recoveredSteps: 2,
      unresolvedSteps: 0,
      answerAffected: false,
      turnCompleted: true,
      ...over,
    });

    it("succeeded turn with nothing affecting the answer: secondary 'handled' copy", () => {
      renderIntl(<StepSummary steps={issues} stepOutcome={outcome({})} onExpand={() => {}} />);
      const badge = screen.getByTestId("step-summary-issues");
      expect(badge.textContent).toBe("2 个步骤曾失败，后续调用已成功");
      expect(badge.getAttribute("data-tone")).toBe("secondary");
    });

    it("warning tone only when an unresolved issue affected the answer", () => {
      renderIntl(
        <StepSummary steps={issues} stepOutcome={outcome({ recoveredSteps: 1, unresolvedSteps: 1, answerAffected: true })} onExpand={() => {}} />,
        { locale: "en-US" },
      );
      const badge = screen.getByTestId("step-summary-issues");
      expect(badge.textContent).toBe("1 step didn't finish, so the answer may be incomplete");
      expect(badge.getAttribute("data-tone")).toBe("warning");
      expect(badge.className).toBe("ui-card__line-warn");
      expect(screen.getByTestId("step-summary").className).toContain("ui-card--icon-warning");
    });

    it("old turns without the field keep the neutral local count", () => {
      renderIntl(<StepSummary steps={issues} onExpand={() => {}} />);
      const badge = screen.getByTestId("step-summary-issues");
      expect(badge.textContent).toBe("2 个步骤遇到问题");
      expect(badge.getAttribute("data-tone")).toBe("secondary");
    });

    it("pluralizes the handled copy in English", () => {
      renderIntl(<StepSummary steps={issues} stepOutcome={outcome({})} onExpand={() => {}} />, { locale: "en-US" });
      expect(screen.getByTestId("step-summary-issues").textContent).toBe("Later calls succeeded after 2 earlier steps failed");
    });
  });

  describe("a turn that ended without an answer (spec §8)", () => {
    const ran = [
      step({ stepId: "a", state: "done", toolName: "bi.heavy_compute" as StepView["toolName"] }),
      step({ stepId: "b", state: "done" }),
      step({ stepId: "c", state: "done" }),
      step({ stepId: "d", state: "failed", toolName: "bi.heavy_compute" as StepView["toolName"] }),
    ];

    it("reads 'Not finished · done / planned steps · duration' in one line, without tool names", () => {
      renderIntl(<StepSummary steps={ran} totalDurationMs={65_000} failed={{ plannedTotal: 5 }} onExpand={() => {}} />);
      const row = screen.getByTestId("step-summary");
      expect(row.getAttribute("data-state")).toBe("failed");
      expect(row.textContent).toContain("未完成");
      expect(screen.getByTestId("step-summary-progress").textContent).toBe(" · 3 / 5 步");
      expect(screen.getByTestId("step-summary-duration").textContent).toBe(" · 1 分 5 秒");
      expect(row.textContent).not.toMatch(/bi\.heavy_compute|临时失败/);
    });

    it("leaves out '/ N' when the plan length is unknown", () => {
      renderIntl(<StepSummary steps={ran} failed={{ plannedTotal: null }} onExpand={() => {}} />, { locale: "en-US" });
      expect(screen.getByTestId("step-summary-progress").textContent).toBe(" · 3 steps");
      expect(screen.getByTestId("step-summary").textContent).toContain("Not finished");
    });

    it("leaves out the step count when no step finished and the plan length is unknown", () => {
      renderIntl(<StepSummary steps={[step({ stepId: "x", state: "failed" })]} failed={{}} onExpand={() => {}} />);
      expect(screen.queryByTestId("step-summary-progress")).toBeNull();
      expect(screen.getByTestId("step-summary").textContent).toContain("未完成");
    });

    it("expands the full trace on click", () => {
      const onExpand = vi.fn();
      renderIntl(<StepSummary steps={ran} failed={{}} onExpand={onExpand} />);
      fireEvent.click(screen.getByTestId("step-summary"));
      expect(onExpand).toHaveBeenCalledTimes(1);
    });
  });

  describe("duration copy: human-readable and localized, never raw milliseconds (spec §8)", () => {
    it.each([
      ["zh-CN", 400, "不到 1 秒"],
      ["zh-CN", 18_000, "18 秒"],
      ["zh-CN", 72_000, "1 分 12 秒"],
      ["zh-CN", 120_000, "2 分"],
      ["en-US", 400, "under 1 s"],
      ["en-US", 72_000, "1 min 12 s"],
      ["de-DE", 400, "unter 1 Sek."],
      ["de-DE", 72_000, "1 Min. 12 Sek."],
      ["fr-FR", 400, "moins d’1 s"],
      ["fr-FR", 72_000, "1 min 12 s"],
    ] as const)("%s %i ms → %s, on the success and the failed row alike", (locale, ms, text) => {
      const steps = [step({ stepId: "a" })];
      const { unmount } = renderIntl(<StepSummary steps={steps} totalDurationMs={ms} onExpand={() => {}} />, { locale });
      expect(screen.getByTestId("step-summary-duration").textContent).toBe(` · ${text}`);
      unmount();
      renderIntl(<StepSummary steps={steps} totalDurationMs={ms} failed={{}} onExpand={() => {}} />, { locale });
      expect(screen.getByTestId("step-summary-duration").textContent).toBe(` · ${text}`);
      expect(screen.getByTestId("step-summary").textContent).not.toMatch(/\d\s*ms\b/);
    });
  });
});
