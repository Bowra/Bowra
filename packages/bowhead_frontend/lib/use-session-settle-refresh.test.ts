import { renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useSessionSettleRefresh } from "./use-session-settle-refresh";

describe("useSessionSettleRefresh", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("re-reads with backoff while the finished session still reads active, then stops", () => {
    const refresh = vi.fn();
    const { rerender } = renderHook((props: { rowState: string; refreshKey: number }) =>
      useSessionSettleRefresh({ finished: true, turnId: "t_1", refresh, ...props }), { initialProps: { rowState: "active", refreshKey: 0 } });
    vi.advanceTimersByTime(1000);
    expect(refresh).toHaveBeenCalledTimes(1);
    rerender({ rowState: "active", refreshKey: 1 });
    vi.advanceTimersByTime(1999);
    expect(refresh).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(1);
    expect(refresh).toHaveBeenCalledTimes(2);
    rerender({ rowState: "done", refreshKey: 2 });
    vi.advanceTimersByTime(60_000);
    expect(refresh).toHaveBeenCalledTimes(2);
  });

  it("gives up after four re-reads and does nothing while the turn runs", () => {
    const refresh = vi.fn();
    const { rerender } = renderHook((props: { refreshKey: number; finished: boolean }) =>
      useSessionSettleRefresh({ turnId: "t_2", rowState: "active", refresh, ...props }), { initialProps: { refreshKey: 0, finished: false } });
    vi.advanceTimersByTime(60_000);
    expect(refresh).not.toHaveBeenCalled();
    for (let key = 0; key < 6; key += 1) {
      rerender({ refreshKey: key, finished: true });
      vi.advanceTimersByTime(20_000);
    }
    expect(refresh).toHaveBeenCalledTimes(4);
  });
});
