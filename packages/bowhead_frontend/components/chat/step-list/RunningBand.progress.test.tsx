/**
 * Agent run limits, phase 3: after a minute without a new step the band says which agent is
 * still running, at which step, and for how long (from the transient `turn.progress` frame).
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
  it("names the agent and its step only after a minute without a new step", () => {
    vi.useFakeTimers();
    const startedAt = Date.now();
    const { container } = render(<RunningBand steps={steps} running startedAt={startedAt} agentProgress={one} />);
    expect(actionText(container)).not.toContain("still_running_progress");

    act(() => {
      vi.advanceTimersByTime(PROGRESS_QUIET_MS + 1_000);
    });
    expect(actionText(container)).toBe("still_running_progress:数据分析专家,37,1");
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

  it("shows nothing extra without a progress frame", () => {
    vi.useFakeTimers();
    const { container } = render(<RunningBand steps={steps} running startedAt={Date.now()} agentProgress={null} />);
    act(() => {
      vi.advanceTimersByTime(PROGRESS_QUIET_MS + 1_000);
    });
    expect(actionText(container)).not.toContain("still_running");
  });
});
