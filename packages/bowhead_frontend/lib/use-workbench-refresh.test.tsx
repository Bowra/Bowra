import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CI_MERGE_POLL_WINDOW_MS } from "./agent-ci-merges";
import { useMergeWindowClock, useWorkbenchRefresh, WORKBENCH_POLL_MS, workbenchShouldPoll } from "./use-workbench-refresh";

function setVisibility(state: "visible" | "hidden") {
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => state });
  document.dispatchEvent(new Event("visibilitychange"));
}

describe("workbenchShouldPoll", () => {
  const running = (status: string) => ({ active_execution_summary: { status } as never });

  it("polls while a comparison is queued, running or finalizing", () => {
    for (const status of ["queued", "running", "finalizing"]) expect(workbenchShouldPoll(running(status), [], 0)).toBe(true);
    for (const status of ["completed", "failed", "cancelled"]) expect(workbenchShouldPoll(running(status), [], 0)).toBe(false);
    expect(workbenchShouldPoll(null, [], 0)).toBe(false);
  });

  it("polls for ten minutes after a CI merge that is still waiting for its candidate", () => {
    const idle = { active_execution_summary: null };
    expect(workbenchShouldPoll(idle, [{ mergedAt: 1_000 }], 1_000 + CI_MERGE_POLL_WINDOW_MS - 1)).toBe(true);
    expect(workbenchShouldPoll(idle, [{ mergedAt: 1_000 }], 1_000 + CI_MERGE_POLL_WINDOW_MS)).toBe(false);
  });
});

describe("useWorkbenchRefresh", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    setVisibility("visible");
  });

  afterEach(() => {
    vi.useRealTimers();
    setVisibility("visible");
  });

  it("polls every five seconds and stops while the page is hidden", () => {
    const refresh = vi.fn();
    renderHook(() => useWorkbenchRefresh({ refresh, activationKey: "release", polling: true }));
    expect(refresh).not.toHaveBeenCalled();
    act(() => { vi.advanceTimersByTime(WORKBENCH_POLL_MS * 2); });
    expect(refresh).toHaveBeenCalledTimes(2);

    act(() => setVisibility("hidden"));
    act(() => { vi.advanceTimersByTime(WORKBENCH_POLL_MS * 4); });
    expect(refresh).toHaveBeenCalledTimes(2);

    // Coming back refetches at once and resumes polling.
    act(() => setVisibility("visible"));
    expect(refresh).toHaveBeenCalledTimes(3);
    act(() => { vi.advanceTimersByTime(WORKBENCH_POLL_MS); });
    expect(refresh).toHaveBeenCalledTimes(4);
  });

  it("doesn't poll when nothing is running but still refetches on tab activation and visibility", () => {
    const refresh = vi.fn();
    const { rerender } = renderHook(
      ({ tab }) => useWorkbenchRefresh({ refresh, activationKey: tab, polling: false }),
      { initialProps: { tab: "config" } },
    );
    act(() => { vi.advanceTimersByTime(WORKBENCH_POLL_MS * 3); });
    expect(refresh).not.toHaveBeenCalled();

    rerender({ tab: "release" });
    expect(refresh).toHaveBeenCalledTimes(1);
    rerender({ tab: "release" });
    expect(refresh).toHaveBeenCalledTimes(1);

    act(() => setVisibility("hidden"));
    expect(refresh).toHaveBeenCalledTimes(1);
    act(() => setVisibility("visible"));
    expect(refresh).toHaveBeenCalledTimes(2);
  });

  it("stops polling once the condition clears", () => {
    const refresh = vi.fn();
    const { rerender } = renderHook(
      ({ polling }) => useWorkbenchRefresh({ refresh, activationKey: "release", polling }),
      { initialProps: { polling: true } },
    );
    act(() => { vi.advanceTimersByTime(WORKBENCH_POLL_MS); });
    expect(refresh).toHaveBeenCalledTimes(1);
    rerender({ polling: false });
    act(() => { vi.advanceTimersByTime(WORKBENCH_POLL_MS * 5); });
    expect(refresh).toHaveBeenCalledTimes(1);
  });
});

describe("useMergeWindowClock", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    setVisibility("visible");
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("closes the merge window by wall clock even when every refresh fails", () => {
    // A failed quiet refresh keeps what is on screen, so it changes nothing the hook could observe.
    const refresh = vi.fn();
    const idle = { active_execution_summary: null };
    // A stable `waiting` list: failed refreshes change nothing the component renders from.
    const waiting = [{ mergedAt: Date.now() }];
    renderHook(() => {
      const clock = useMergeWindowClock(waiting);
      useWorkbenchRefresh({ refresh, activationKey: "release", polling: workbenchShouldPoll(idle, waiting, clock) });
    });
    act(() => { vi.advanceTimersByTime(WORKBENCH_POLL_MS * 2); });
    expect(refresh).toHaveBeenCalledTimes(2);
    act(() => { vi.advanceTimersByTime(CI_MERGE_POLL_WINDOW_MS); });
    const calls = refresh.mock.calls.length;
    act(() => { vi.advanceTimersByTime(WORKBENCH_POLL_MS * 10); });
    expect(refresh).toHaveBeenCalledTimes(calls);
  });

  it("advances to the next window when merges end at different times", () => {
    const start = Date.now();
    const waiting = [{ mergedAt: start }, { mergedAt: start + 60_000 }];
    const { result } = renderHook(() => workbenchShouldPoll(null, waiting, useMergeWindowClock(waiting)));
    act(() => { vi.advanceTimersByTime(CI_MERGE_POLL_WINDOW_MS); });
    expect(result.current).toBe(true);
    act(() => { vi.advanceTimersByTime(60_000); });
    expect(result.current).toBe(false);
  });
});
