import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { queryResults, ResultsApiError } from "./results-api";
import {
  RESULTS_QUICK_SEARCH_DEBOUNCE_MS,
  useResultsQuickQuery,
} from "./use-results-quick-query";

vi.mock("./results-api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./results-api")>()),
  queryResults: vi.fn(),
}));

const response = (key: string) => ({
  dto_version: 1 as const,
  capability: { complete_list: true as const, scale_nav: true },
  snapshot_token: "snapshot",
  items: [{ key }] as never[],
  authorized_total: 20,
  matched_total: 12,
  group_counts: {
    deliverables: 8,
    evidence: 12,
    data: 8,
    code: 2,
    input: 2,
  },
  loaded_count: 1,
  page_start: 0,
  next_cursor: null,
  previous_cursor: null,
  has_more: true,
  projection_status: {
    report_relations: "ready" as const,
    execution_relations: "unavailable" as const,
    export_relations: "degraded" as const,
  },
  projection_revision: "rev",
  new_results: { count: 0, projection_changed: false },
  resolved_current_key: null,
});

beforeEach(() => {
  vi.useFakeTimers();
  vi.mocked(queryResults).mockReset();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("useResultsQuickQuery", () => {
  it("requests ten defaults immediately and eight search hits after 200ms", async () => {
    vi.mocked(queryResults).mockResolvedValue(response("report:cv_1"));
    const { rerender } = renderHook((props: { query: string }) => (
      useResultsQuickQuery({
        enabled: true,
        open: true,
        sessionId: "s_1",
        scope: "session",
        currentKey: "report:cv_1",
        recentKeys: ["artifact:a"],
        query: props.query,
      })
    ), { initialProps: { query: "" } });

    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(queryResults).toHaveBeenLastCalledWith(expect.objectContaining({
      request: expect.objectContaining({ surface: "quick", page_size: 10 }),
    }));

    rerender({ query: " revenue " });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(RESULTS_QUICK_SEARCH_DEBOUNCE_MS - 1);
    });
    expect(queryResults).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(queryResults).toHaveBeenLastCalledWith(expect.objectContaining({
      request: expect.objectContaining({
        surface: "quick",
        query: "revenue",
        page_size: 8,
        current_key: "report:cv_1",
        recent_keys: ["artifact:a"],
      }),
    }));
  });

  it("does not request while closed and clears visible state on close", async () => {
    vi.mocked(queryResults).mockResolvedValue(response("artifact:a"));
    const { result, rerender } = renderHook((props: { open: boolean }) => (
      useResultsQuickQuery({
        enabled: true,
        open: props.open,
        sessionId: "s_1",
        scope: "session",
        currentKey: null,
        recentKeys: [],
        query: "",
      })
    ), { initialProps: { open: false } });
    expect(queryResults).not.toHaveBeenCalled();

    rerender({ open: true });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
      await Promise.resolve();
    });
    expect(result.current.items).toHaveLength(1);
    rerender({ open: false });
    expect(result.current.items).toEqual([]);
  });

  it("keeps the latest query when an older response arrives late", async () => {
    const resolvers: Array<(value: ReturnType<typeof response>) => void> = [];
    vi.mocked(queryResults).mockImplementation(() => new Promise((resolve) => {
      resolvers.push(resolve);
    }));
    const { result, rerender } = renderHook((props: { query: string }) => (
      useResultsQuickQuery({
        enabled: true,
        open: true,
        sessionId: "s_1",
        scope: "session",
        currentKey: null,
        recentKeys: [],
        query: props.query,
      })
    ), { initialProps: { query: "old" } });
    await act(async () => { await vi.advanceTimersByTimeAsync(200); });
    rerender({ query: "new" });
    await act(async () => { await vi.advanceTimersByTimeAsync(200); });

    await act(async () => { resolvers[1](response("artifact:new")); });
    expect(result.current.items[0]?.key).toBe("artifact:new");
    await act(async () => { resolvers[0](response("artifact:old")); });
    expect(result.current.items[0]?.key).toBe("artifact:new");
  });

  it("purges scope state and invokes the recent cleanup on authorization loss", async () => {
    const onScopePurged = vi.fn();
    vi.mocked(queryResults).mockRejectedValue(
      new ResultsApiError("results_scope_unavailable", 403),
    );
    const { result } = renderHook(() => useResultsQuickQuery({
      enabled: true,
      open: true,
      sessionId: "s_1",
      scope: "report",
      currentKey: null,
      recentKeys: [],
      query: "",
      onScopePurged,
    }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
      await Promise.resolve();
    });
    expect(result.current.loading).toBe(false);
    expect(result.current.error).toBe("results_scope_unavailable");
    expect(onScopePurged).toHaveBeenCalledTimes(1);
  });
});
