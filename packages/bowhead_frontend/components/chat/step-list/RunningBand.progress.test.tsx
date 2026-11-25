/**
 * Agent run limits, phase 3: after a minute without a new step the band says which agent is
 * still running and for how long (from the transient `turn.progress` frame). It gives no step number: the frame's
 * count is the helper's model calls, which disagreed with the band's own "第 N 步" (an observed deployment).
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render } from "@testing-library/react";

import { PROGRESS_QUIET_MS, RunningBand } from "./RunningBand";
import type { StepView } from "./types";

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string, vals?: Record<string, unknown>) =>
    vals ? `${key}:${Object.values(vals).join(",")}` : key,
}));

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

const steps: StepView[] = [];
const actionText = (container: HTMLElement): string => container.querySelector(".bh-band__act")?.textContent ?? "";
const one = { agents: [{ subagentId: "sub_1", profile: "compute", displayName: "数据分析专家", step: 37 }] };

describe("RunningBand progress line", () => {
  it("names the agent, without a step number, only after a minute without a new step", () => {
    vi.useFakeTimers();
    const startedAt = Date.now();
    const { container } = render(<RunningBand steps={steps} running startedAt={startedAt} agentProgress={one} />);
    expect(actionText(container)).not.toContain("still_running_progress");

    act(() => {
      vi.advanceTimersByTime(PROGRESS_QUIET_MS + 1_000);
    });
    expect(actionText(container)).toBe("still_running_progress:数据分析专家,1");
  });

  it("words a built-in agent whose display name is its identifier as the trace does", () => {
    // an observed deployment dogfood (an observed task): the band read "仍在运行 · compute · 第 10 步" for the built-in
    // compute helper. Mutation: fall back to `profile` again → this test fails.
    vi.useFakeTimers();
    const builtin = { agents: [{ subagentId: "sub_1", profile: "compute", displayName: "compute", step: 10 }] };
    const { container } = render(<RunningBand steps={steps} running startedAt={Date.now()} agentProgress={builtin} />);
    act(() => {
      vi.advanceTimersByTime(PROGRESS_QUIET_MS + 1_000);
    });
    expect(actionText(container)).toMatch(/^still_running_progress:/);
    expect(actionText(container)).not.toContain("compute");
  });

  it("summarizes several agents running in parallel", () => {
    vi.useFakeTimers();
    const many = { agents: [...one.agents, { subagentId: "sub_2", profile: "kb-analyst", displayName: "", step: 4 }] };
    const { container } = render(<RunningBand steps={steps} running startedAt={Date.now()} agentProgress={many} />);
    act(() => {
      vi.advanceTimersByTime(PROGRESS_QUIET_MS + 1_000);
    });
    expect(actionText(container)).toBe("still_running_many:2,1");
  });

  it("leaves out an agent whose run has ended", () => {
    // an observed deployment dogfood (s_424e242beb0f): the frame stops once no agent runs, so the last one stayed and the
    // band read "仍在运行 · 数据与计算分析师 · 第 2 步" while the Lead wrote its answer.
    // Mutation: read every agent of the frame again → this test fails.
    vi.useFakeTimers();
    const many = { agents: [...one.agents, { subagentId: "sub_2", profile: "kb-analyst", displayName: "知识分析师", step: 4 }] };
    const ended = { sub_1: { status: "succeeded" as const } };
    const { container, rerender } = render(
      <RunningBand steps={steps} running startedAt={Date.now()} agentProgress={many} peerTerminalById={ended} />,
    );
    act(() => {
      vi.advanceTimersByTime(PROGRESS_QUIET_MS + 1_000);
    });
    expect(actionText(container)).toBe("still_running_progress:知识分析师,1");

    rerender(
      <RunningBand
        steps={steps} running startedAt={Date.now()} agentProgress={many}
        peerTerminalById={{ ...ended, sub_2: { status: "failed" as const } }}
      />,
    );
    expect(actionText(container)).not.toContain("still_running");
  });

  it("keeps an ended agent that runs again in the same turn", () => {
    vi.useFakeTimers();
    const again = { sub_1: { status: "succeeded" as const, rerunning: true } };
    const { container } = render(
      <RunningBand steps={steps} running startedAt={Date.now()} agentProgress={one} peerTerminalById={again} />,
    );
    act(() => {
      vi.advanceTimersByTime(PROGRESS_QUIET_MS + 1_000);
    });
    expect(actionText(container)).toBe("still_running_progress:数据分析专家,1");
  });

  it("shows nothing extra without a progress frame", () => {
    vi.useFakeTimers();
    const { container } = render(<RunningBand steps={steps} running startedAt={Date.now()} agentProgress={null} />);
    act(() => {
      vi.advanceTimersByTime(PROGRESS_QUIET_MS + 1_000);
    });
    expect(actionText(container)).not.toContain("still_running");
  });
});
