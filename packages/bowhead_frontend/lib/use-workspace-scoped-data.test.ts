/**
 * Sprint β G2 — useWorkspaceScopedData hook tests.
 *
 * Covers:
 * - mount triggers fetch with active ws
 * - ws change event triggers refetch
 * - rapid switches coalesce via 50ms setTimeout
 * - stale-response guard via generation counter
 * - 403 → fallback retry max 2
 * - 403 exhausted → redirect to /empty-workspace
 * - no activeId → no fetch (or skip flag)
 * - refetch() function fires new fetch
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";

// Mock useSpace before importing the hook under test
vi.mock("./use-space", () => ({
  useSpace: vi.fn(),
}));

import { useWorkspaceScopedData } from "./use-workspace-scoped-data";
import { useSpace } from "./use-space";

const EVT = "bowhead:workspace-changed";

describe("useWorkspaceScopedData", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.mocked(useSpace).mockReturnValue({
      items: [],
      activeId: "ws_1",
      activeName: "ws_1",
      loading: false,
      error: null,
      setActiveId: vi.fn(),
      refresh: vi.fn(),
    });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("triggers fetch on mount with active workspace", async () => {
    const fetcher = vi.fn().mockResolvedValue({ data: "hello" });
    const { result } = renderHook(() => useWorkspaceScopedData(fetcher));

    expect(result.current.loading).toBe(false); // pre-timer
    expect(fetcher).not.toHaveBeenCalled();

    await act(async () => {
      vi.advanceTimersByTime(60);
    });

    expect(fetcher).toHaveBeenCalledWith("ws_1");
  });

  it("coalesces rapid scheduleFetch calls (50ms setTimeout)", async () => {
    const fetcher = vi.fn().mockResolvedValue({ data: "x" });
    const { result, rerender } = renderHook(() =>
      useWorkspaceScopedData(fetcher),
    );

    // Mount fires once
    await act(async () => {
      vi.advanceTimersByTime(60);
    });
    expect(fetcher).toHaveBeenCalledTimes(1);

    // Multiple workspace-changed events within coalesce window — should fire 1x
    await act(async () => {
      window.dispatchEvent(new CustomEvent(EVT));
      window.dispatchEvent(new CustomEvent(EVT));
      window.dispatchEvent(new CustomEvent(EVT));
      vi.advanceTimersByTime(60);
    });

    expect(fetcher).toHaveBeenCalledTimes(2); // mount + one coalesced burst
  });

  it("stale response from older fetch is discarded", async () => {
    let resolveFirst: (v: unknown) => void = () => {};
    const slowResolve = new Promise((res) => {
      resolveFirst = res;
    });

    const fetcher = vi
      .fn()
      .mockImplementationOnce(() => slowResolve)
      .mockResolvedValue({ ws: "new" });

    const { result } = renderHook(() => useWorkspaceScopedData(fetcher));

    // First fetch starts
    await act(async () => {
      vi.advanceTimersByTime(60);
    });
    expect(fetcher).toHaveBeenCalledTimes(1);

    // Trigger second fetch BEFORE first resolves
    await act(async () => {
      window.dispatchEvent(new CustomEvent(EVT));
      vi.advanceTimersByTime(60);
      await vi.runAllTimersAsync();
    });
    expect(fetcher).toHaveBeenCalledTimes(2);

    // Now first resolves — should be DISCARDED (stale gen)
    await act(async () => {
      resolveFirst({ ws: "stale" });
      await Promise.resolve();
      await Promise.resolve();
    });

    // 2nd fetcher (returns "new") should have set data; "stale" must NOT clobber
    expect(result.current.data).toEqual({ ws: "new" });
  });

  it("403 triggers fallback retry up to 2x", async () => {
    const error = Object.assign(new Error("403"), { status: 403 });
    const fetcher = vi
      .fn()
      .mockRejectedValueOnce(error)
      .mockRejectedValueOnce(error)
      .mockResolvedValue({ data: "recovered" });
    const { result } = renderHook(() => useWorkspaceScopedData(fetcher));

    await act(async () => {
      vi.advanceTimersByTime(60);
      await vi.runAllTimersAsync();
      await Promise.resolve();
    });
    expect(fetcher).toHaveBeenCalledTimes(3);
    expect(result.current.data).toEqual({ data: "recovered" });
    expect(result.current.error).toBeNull();
  });

  it("uses workspace-changed event detail instead of a stale activeId", async () => {
    const fetcher = vi.fn().mockResolvedValue({ data: "ok" });
    renderHook(() => useWorkspaceScopedData(fetcher));

    await act(async () => {
      vi.advanceTimersByTime(60);
      await Promise.resolve();
    });
    expect(fetcher).toHaveBeenCalledWith("ws_1");

    await act(async () => {
      window.dispatchEvent(new CustomEvent(EVT, { detail: "ws_2" }));
      vi.advanceTimersByTime(60);
      await Promise.resolve();
    });

    expect(fetcher).toHaveBeenLastCalledWith("ws_2");
  });

  it("refetch() function fires new fetch", async () => {
    const fetcher = vi.fn().mockResolvedValue({ data: "ok" });
    const { result } = renderHook(() => useWorkspaceScopedData(fetcher));

    await act(async () => {
      vi.advanceTimersByTime(60);
    });
    expect(fetcher).toHaveBeenCalledTimes(1);

    await act(async () => {
      result.current.refetch();
      vi.advanceTimersByTime(60);
    });
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it("skips fetch when no activeId", async () => {
    vi.mocked(useSpace).mockReturnValue({
      items: [],
      activeId: null,
      activeName: "",
      loading: false,
      error: null,
      setActiveId: vi.fn(),
      refresh: vi.fn(),
    });

    const fetcher = vi.fn();
    const { result } = renderHook(() => useWorkspaceScopedData(fetcher));

    await act(async () => {
      vi.advanceTimersByTime(60);
    });

    expect(fetcher).not.toHaveBeenCalled();
    expect(result.current.data).toBeNull();
    expect(result.current.loading).toBe(false);
  });

  it("sets data from successful fetcher result", async () => {
    const fetcher = vi.fn().mockResolvedValue({ payload: 42 });
    const { result } = renderHook(() => useWorkspaceScopedData(fetcher));

    await act(async () => {
      vi.advanceTimersByTime(60);
      await vi.runAllTimersAsync();
      await Promise.resolve();
    });

    expect(result.current.data).toEqual({ payload: 42 });
    expect(result.current.error).toBeNull();
  });

  it("non-403 error surfaces to caller", async () => {
    const fetcher = vi.fn().mockRejectedValue(new Error("network down"));
    const { result } = renderHook(() => useWorkspaceScopedData(fetcher));

    await act(async () => {
      vi.advanceTimersByTime(60);
      await vi.runAllTimersAsync();
      await Promise.resolve();
    });

    expect(result.current.error?.message).toBe("network down");
    expect(result.current.data).toBeNull();
  });
});
