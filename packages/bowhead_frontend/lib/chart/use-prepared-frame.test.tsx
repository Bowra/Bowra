import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { usePreparedFrame } from "./use-prepared-frame";

describe("prepared playback lifecycle", () => {
  const host = { current: document.createElement("section") };
  const keys = ["2026-01", "2026-02", "2026-03"];
  beforeEach(() => { vi.useFakeTimers(); Object.defineProperty(document, "hidden", { configurable: true, value: false }); });
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });
  it("starts paused, advances supplied keys once, stops at the last frame and cleans up on unmount", () => {
    const { result, unmount } = renderHook(() => usePreparedFrame(true, "root1", keys, keys[1], host));
    expect(result.current).toMatchObject({ key: keys[1], playing: false });
    act(() => result.current.seek(0)); act(() => result.current.play());
    act(() => vi.advanceTimersByTime(1000)); expect(result.current.key).toBe(keys[1]);
    act(() => vi.advanceTimersByTime(1000)); expect(result.current).toMatchObject({ key: keys[2], playing: false });
    expect(vi.getTimerCount()).toBe(0);
    act(() => result.current.seek(0)); act(() => result.current.play());
    unmount(); expect(vi.getTimerCount()).toBe(0);
  });
  it("pauses on seek, hidden/offscreen transitions and export restoration without automatically resuming", () => {
    let intersection: (entries: Array<{ isIntersecting: boolean }>) => void = () => {};
    vi.stubGlobal("IntersectionObserver", class { constructor(callback: typeof intersection) { intersection = callback; } observe() {} disconnect() {} });
    const { result } = renderHook(() => usePreparedFrame(true, "root1", keys, keys[0], host));
    act(() => result.current.play()); act(() => result.current.seek(1));
    expect(result.current.playing).toBe(false);
    act(() => result.current.play());
    act(() => { Object.defineProperty(document, "hidden", { configurable: true, value: true }); document.dispatchEvent(new Event("visibilitychange")); });
    expect(result.current.playing).toBe(false);
    act(() => { Object.defineProperty(document, "hidden", { configurable: true, value: false }); document.dispatchEvent(new Event("visibilitychange")); vi.advanceTimersByTime(2000); });
    expect(result.current.key).toBe(keys[1]);
    act(() => result.current.play()); act(() => intersection([{ isIntersecting: false }]));
    expect(result.current.playing).toBe(false);
    act(() => intersection([{ isIntersecting: true }])); expect(result.current.playing).toBe(false);
    act(() => result.current.reset()); expect(result.current).toMatchObject({ key: keys[0], playing: false });
  });
  it("invalidates the old clock and scope when root revision or dataset access changes", () => {
    const { result, rerender } = renderHook(({ enabled, epoch }) => usePreparedFrame(enabled, epoch, keys, keys[1], host), { initialProps: { enabled: true, epoch: "root1" } });
    act(() => result.current.seek(0)); act(() => result.current.play());
    rerender({ enabled: true, epoch: "root2" }); expect(result.current).toMatchObject({ key: keys[1], playing: false });
    act(() => result.current.seek(0)); act(() => result.current.play());
    rerender({ enabled: false, epoch: "root2" }); expect(result.current.playing).toBe(false);
    act(() => vi.advanceTimersByTime(2000)); expect(result.current.key).toBe(keys[1]);
  });
});
