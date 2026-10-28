import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const toastMock = vi.hoisted(() => vi.fn());

vi.mock("@/lib/use-toast", () => ({ toast: toastMock }));

import { useReportAutoExport } from "./use-report-auto-export";

describe("useReportAutoExport", () => {
  beforeEach(() => {
    toastMock.mockReset();
    window.history.replaceState({}, "", "/?session=s_target&report=1&export=xlsx");
  });

  afterEach(() => {
    cleanup();
    window.history.replaceState({}, "", "/");
    vi.useRealTimers();
  });

  it("waits for the target session without consuming the export intent", () => {
    const triggerExport = vi.fn(async () => true);

    renderHook(() => useReportAutoExport({
      canvasId: null,
      sessionId: null,
      emptyMessage: "没有可见内容",
      triggerExport,
    }));

    expect(new URL(window.location.href).searchParams.get("export")).toBe("xlsx");
    expect(toastMock).not.toHaveBeenCalled();
    expect(triggerExport).not.toHaveBeenCalled();
  });

  it("preserves export intent while capabilities are unresolved and exports once after recovery", async () => {
    const triggerExport = vi.fn(async () => true);
    const { rerender } = renderHook(({ enabled }) => useReportAutoExport({
      enabled,
      canvasId: "cv_target",
      sessionId: "s_target",
      emptyMessage: "没有可见内容",
      triggerExport,
    }), { initialProps: { enabled: false } });
    expect(new URL(window.location.href).searchParams.get("export")).toBe("xlsx");
    expect(triggerExport).not.toHaveBeenCalled();
    rerender({ enabled: true });
    await waitFor(() => expect(triggerExport).toHaveBeenCalledTimes(1));
    expect(new URL(window.location.href).searchParams.has("export")).toBe(false);
  });

  it("consumes a hydrated empty report, shows an error, and sends no export request", async () => {
    const triggerExport = vi.fn(async () => true);

    renderHook(() => useReportAutoExport({
      canvasId: null,
      sessionId: "s_target",
      emptyMessage: "没有可见内容",
      triggerExport,
    }));

    await waitFor(() => expect(toastMock).toHaveBeenCalledWith({
      kind: "error",
      text: "没有可见内容",
    }));
    expect(new URL(window.location.href).searchParams.has("export")).toBe(false);
    expect(triggerExport).not.toHaveBeenCalled();
  });

  it("consumes a real report and starts the V2-ready retry path", async () => {
    const triggerExport = vi.fn(async () => true);

    renderHook(() => useReportAutoExport({
      canvasId: "cv_target",
      sessionId: "s_target",
      emptyMessage: "没有可见内容",
      triggerExport,
    }));

    await waitFor(() => expect(triggerExport).toHaveBeenCalledWith(
      "xlsx",
      { retryTransient: true },
    ));
    expect(new URL(window.location.href).searchParams.has("export")).toBe(false);
    expect(toastMock).not.toHaveBeenCalled();
  });

  it("consumes an unsupported format without exporting or retrying on refresh", () => {
    window.history.replaceState({}, "", "/?session=s_target&report=1&export=docx");
    const triggerExport = vi.fn(async () => true);

    renderHook(() => useReportAutoExport({
      canvasId: "cv_target",
      sessionId: "s_target",
      emptyMessage: "没有可见内容",
      triggerExport,
    }));

    expect(new URL(window.location.href).searchParams.has("export")).toBe(false);
    expect(toastMock).not.toHaveBeenCalled();
    expect(triggerExport).not.toHaveBeenCalled();
  });

  it("retries transient render readiness and then succeeds without legacy fallback", async () => {
    vi.useFakeTimers();
    const triggerExport = vi.fn()
      .mockResolvedValueOnce(false)
      .mockResolvedValueOnce(true);

    renderHook(() => useReportAutoExport({
      canvasId: "cv_target",
      sessionId: "s_target",
      emptyMessage: "没有可见内容",
      triggerExport,
    }));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_500);
    });

    expect(triggerExport).toHaveBeenNthCalledWith(1, "xlsx", { retryTransient: true });
    expect(triggerExport).toHaveBeenNthCalledWith(2, "xlsx", { retryTransient: true });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(triggerExport).toHaveBeenCalledTimes(2);
  });
});
