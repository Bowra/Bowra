/**
 * Global Toast data layer: error Toasts persist until manually dismissed, so repeated identical errors must coalesce their counts,
 * with a maximum number of entries instead of accumulating indefinitely. Non-error Toast behavior is unchanged.
 */
import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { MAX_ERROR_TOASTS, _clearAllToasts, dismissToast, toast, useToasts } from "./use-toast";

afterEach(() => {
  _clearAllToasts();
  vi.useRealTimers();
});

describe("error toasts", () => {
  it("merges an identical error that is still showing and counts it", () => {
    vi.useFakeTimers();
    const view = renderHook(() => useToasts());
    const ids = Array.from({ length: 12 }, () => toast({ kind: "error", text: "保存失败：网络错误" }));
    vi.advanceTimersByTime(10 * 60_000);
    view.rerender();

    expect(view.result.current).toHaveLength(1);
    expect(view.result.current[0]).toMatchObject({ kind: "error", text: "保存失败：网络错误", count: 12 });
    expect(new Set(ids).size).toBe(1);
  });

  it("starts over once the merged error has been dismissed", () => {
    const view = renderHook(() => useToasts());
    const first = toast({ kind: "error", text: "x" });
    toast({ kind: "error", text: "x" });
    dismissToast(first);
    toast({ kind: "error", text: "x" });
    view.rerender();

    expect(view.result.current).toHaveLength(1);
    expect(view.result.current[0]?.count).toBeUndefined();
  });

  it("keeps errors with different text or with their own callbacks apart", () => {
    const view = renderHook(() => useToasts());
    toast({ kind: "error", text: "a" });
    toast({ kind: "error", text: "b" });
    toast({ kind: "error", text: "b", action: { label: "重试", onAction: () => undefined } });
    view.rerender();

    expect(view.result.current.map((item) => item.text)).toEqual(["a", "b", "b"]);
  });

  it("caps how many errors stay on screen, dropping the oldest", () => {
    const view = renderHook(() => useToasts());
    for (let index = 0; index < MAX_ERROR_TOASTS + 2; index += 1) toast({ kind: "error", text: `错误 ${index}` });
    toast({ kind: "info", text: "提示" });
    view.rerender();

    const errors = view.result.current.filter((item) => item.kind === "error");
    expect(errors).toHaveLength(MAX_ERROR_TOASTS);
    expect(errors.map((item) => item.text)).toEqual(
      Array.from({ length: MAX_ERROR_TOASTS }, (_, index) => `错误 ${index + 2}`),
    );
    expect(view.result.current.some((item) => item.kind === "info")).toBe(true);
  });
});

describe("non-error toasts", () => {
  it("are neither merged nor capped", () => {
    vi.useFakeTimers();
    const view = renderHook(() => useToasts());
    for (let index = 0; index < MAX_ERROR_TOASTS + 2; index += 1) toast({ kind: "success", text: "已保存" });
    view.rerender();
    expect(view.result.current).toHaveLength(MAX_ERROR_TOASTS + 2);
    expect(view.result.current.every((item) => item.count === undefined)).toBe(true);

    vi.advanceTimersByTime(4_000);
    view.rerender();
    expect(view.result.current).toHaveLength(0);
  });
});
