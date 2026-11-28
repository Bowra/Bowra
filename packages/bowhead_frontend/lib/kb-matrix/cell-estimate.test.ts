import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const estimateCell = vi.hoisted(() => vi.fn());
vi.mock("./run-api", () => ({ estimateCell }));

import { _clearCellEstimateCache, useCellRerunCredits } from "./cell-estimate";

beforeEach(() => {
  _clearCellEstimateCache();
  estimateCell.mockReset();
});
afterEach(() => vi.useRealTimers());

describe("useCellRerunCredits", () => {
  it("prices a cell once and reuses the estimate for the same column version", async () => {
    estimateCell.mockResolvedValue({ credits_estimated: 7 });
    const first = renderHook(() => useCellRerunCredits("mx_1", "f1", "c1", 2, false));
    await waitFor(() => expect(first.result.current).toBe(7));
    first.unmount();
    const again = renderHook(() => useCellRerunCredits("mx_1", "f1", "c1", 2, false));
    expect(again.result.current).toBe(7);
    expect(estimateCell).toHaveBeenCalledTimes(1);
    expect(estimateCell).toHaveBeenCalledWith("mx_1", "f1", "c1");
  });

  it("prices again after the column changes, and skips read-only viewers", async () => {
    estimateCell.mockResolvedValueOnce({ credits_estimated: 7 }).mockResolvedValueOnce({ credits_estimated: 14 });
    const view = renderHook(({ pv, cv }) => useCellRerunCredits("mx_1", "f1", "c1", pv, cv), { initialProps: { pv: 2, cv: false } });
    await waitFor(() => expect(view.result.current).toBe(7));
    view.rerender({ pv: 3, cv: true });
    await waitFor(() => expect(view.result.current).toBe(14));
    const viewer = renderHook(() => useCellRerunCredits(null, "f1", "c1", 3, true));
    expect(viewer.result.current).toBeNull();
    expect(estimateCell).toHaveBeenCalledTimes(2);
  });

  it("leaves the price unknown when the estimate fails, and asks again next time", async () => {
    estimateCell.mockRejectedValueOnce(new Error("offline")).mockResolvedValueOnce({ credits_estimated: 5 });
    const first = renderHook(() => useCellRerunCredits("mx_1", "f2", "c1", 1, false));
    await waitFor(() => expect(estimateCell).toHaveBeenCalledTimes(1));
    expect(first.result.current).toBeNull();
    first.unmount();
    const again = renderHook(() => useCellRerunCredits("mx_1", "f2", "c1", 1, false));
    await waitFor(() => expect(again.result.current).toBe(5));
  });
});
