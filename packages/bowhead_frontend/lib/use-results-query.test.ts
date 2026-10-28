import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ResultsApiError,
  type ResultItemDTO,
  type ResultsFiltersDTO,
  type ResultsQueryResponse,
} from "./results-api";
import { useResultsQuery } from "./use-results-query";

const { queryResultsMock } = vi.hoisted(() => ({ queryResultsMock: vi.fn() }));

vi.mock("@/lib/results-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./results-api")>();
  return { ...actual, queryResults: queryResultsMock };
});

function item(key: string, position = 1): ResultItemDTO {
  const artifactId = key.replace("artifact:", "");
  return {
    key,
    object_type: "artifact",
    title: `${artifactId}.csv`,
    primary_group: "evidence",
    secondary_group: "data",
    viewer_profile: "table",
    status: "ready",
    route: { type: "artifact", artifact_id: artifactId, default_mode: "preview" },
    capabilities: ["download", "view_details", "view_raw"],
    relation_roles: [],
    execution: null,
    display_context: {
      source_role: "data_query",
      output_role: null,
      execution_label: null,
      occurred_at: "2026-08-25T00:00:00+00:00",
      duplicate_ordinal: null,
      duplicate_count: null,
    },
    position,
    version: null,
    artifact_metadata: {
      mime: "text/csv",
      size: 10,
      source: "bi_fetch_rows",
      kind: "tool_data",
      origin: "query",
      trust: "trusted",
      created_at: "2026-08-25T00:00:00+00:00",
      producer_kind: "bi_fetch_rows",
    },
  };
}

function page(
  items: ResultItemDTO[],
  values: Partial<ResultsQueryResponse> = {},
): ResultsQueryResponse {
  return {
    dto_version: 1,
    capability: { complete_list: true, scale_nav: false },
    snapshot_token: "snapshot",
    items,
    authorized_total: items.length,
    matched_total: items.length,
    group_counts: {
      deliverables: 0,
      evidence: items.length,
      data: items.length,
      code: 0,
      input: 0,
    },
    loaded_count: items.length,
    page_start: 0,
    next_cursor: null,
    previous_cursor: null,
    has_more: false,
    projection_status: {
      report_relations: "ready",
      execution_relations: "unavailable",
      export_relations: "degraded",
    },
    projection_revision: "revision",
    new_results: { count: null, projection_changed: false },
    resolved_current_key: null,
    ...values,
  };
}

async function flushLoad(): Promise<void> {
  await act(async () => {
    vi.runAllTimers();
    for (let index = 0; index < 5; index += 1) await Promise.resolve();
  });
}

describe("useResultsQuery", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    queryResultsMock.mockReset();
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  it("loads an unselected Explorer root with one base request", async () => {
    queryResultsMock.mockResolvedValueOnce(page([item("artifact:first")]));
    const hook = renderHook(() => useResultsQuery({
      enabled: true,
      query: "",
      scope: "session",
      sessionId: "s_1",
      selectionKey: null,
    }));

    await flushLoad();

    expect(queryResultsMock).toHaveBeenCalledTimes(1);
    expect(queryResultsMock.mock.calls[0]?.[0].request).toMatchObject({
      surface: "explorer",
      page_size: 50,
    });
    expect(queryResultsMock.mock.calls[0]?.[0].request).not.toHaveProperty(
      "anchor_key",
    );
    expect(hook.result.current.items[0]?.key).toBe("artifact:first");
  });

  it("keeps only the latest server search response", async () => {
    const deferred = new Map<string, (value: ResultsQueryResponse) => void>();
    queryResultsMock.mockImplementation(({ request }: { request: { query?: string } }) => (
      new Promise<ResultsQueryResponse>((resolve) => {
        deferred.set(request.query ?? "", resolve);
      })
    ));
    const hook = renderHook(
      ({ query }) => useResultsQuery({
        enabled: true,
        query,
        scope: "session",
        sessionId: "s_1",
      }),
      { initialProps: { query: "first" } },
    );
    await flushLoad();
    hook.rerender({ query: "second" });
    await flushLoad();

    await act(async () => {
      deferred.get("first")?.(page([item("artifact:first")]));
      deferred.get("second")?.(page([item("artifact:second")]));
      await Promise.resolve();
    });

    expect(hook.result.current.items.map((value) => value.key)).toEqual([
      "artifact:second",
    ]);
  });

  it("uses the latest selection when it changes before the search debounce", async () => {
    queryResultsMock.mockResolvedValueOnce(page([item("artifact:first")]));
    const hook = renderHook(
      ({ selectionKey }) => useResultsQuery({
        enabled: true,
        query: "search",
        scope: "session",
        sessionId: "s_1",
        selectionKey,
      }),
      { initialProps: { selectionKey: "artifact:stale" as string | null } },
    );

    hook.rerender({ selectionKey: null });
    await flushLoad();

    expect(queryResultsMock).toHaveBeenCalledTimes(1);
    expect(queryResultsMock.mock.calls[0]?.[0].request).toMatchObject({
      surface: "explorer",
      query: "search",
    });
    expect(queryResultsMock.mock.calls[0]?.[0].request).not.toHaveProperty(
      "anchor_key",
    );
  });

  it("appends cursor pages and preserves them when load-more fails", async () => {
    queryResultsMock
      .mockResolvedValueOnce(page([item("artifact:one")], {
        authorized_total: 2,
        matched_total: 2,
        next_cursor: "next",
        has_more: true,
      }))
      .mockRejectedValueOnce(new ResultsApiError("results_request_failed"));
    const hook = renderHook(() => useResultsQuery({
      enabled: true,
      query: "",
      scope: "session",
      sessionId: "s_1",
    }));
    await flushLoad();
    expect(hook.result.current.items).toHaveLength(1);

    await act(async () => {
      await hook.result.current.loadMore();
    });

    expect(hook.result.current.items.map((value) => value.key)).toEqual([
      "artifact:one",
    ]);
    expect(hook.result.current.error).toBe("results_request_failed");
    expect(hook.result.current.hasMore).toBe(true);
    expect(queryResultsMock.mock.calls[1]?.[0].request).toMatchObject({
      cursor: "next",
      snapshot_token: "snapshot",
    });
  });

  it("keeps the current page while a filter update is pending or fails", async () => {
    let rejectFilter: ((error: unknown) => void) | null = null;
    queryResultsMock
      .mockResolvedValueOnce(page([item("artifact:current")]))
      .mockImplementationOnce(() => new Promise((_resolve, reject) => {
        rejectFilter = reject;
      }));
    const allFilters: ResultsFiltersDTO = {
      lens: "all",
      primary: "all",
      secondary: "all",
      technical_only: false,
    };
    const evidenceFilters: ResultsFiltersDTO = {
      ...allFilters,
      primary: "evidence",
      secondary: "data",
    };
    const hook = renderHook(
      ({ filters }) => useResultsQuery({
        enabled: true,
        filters,
        query: "",
        scope: "session",
        sessionId: "s_1",
      }),
      { initialProps: { filters: allFilters } },
    );
    await flushLoad();

    hook.rerender({ filters: evidenceFilters });
    expect(hook.result.current.items[0]?.key).toBe("artifact:current");
    expect(hook.result.current.completedView?.filters).toEqual(allFilters);
    expect(hook.result.current.loading).toBe(true);
    await flushLoad();
    await act(async () => {
      rejectFilter?.(new ResultsApiError("results_request_failed"));
      await Promise.resolve();
    });

    expect(hook.result.current.items[0]?.key).toBe("artifact:current");
    expect(hook.result.current.error).toBe("results_request_failed");
    expect(hook.result.current.completedView?.filters).toEqual(allFilters);
    expect(queryResultsMock.mock.calls[1]?.[0].request.filters).toEqual(
      evidenceFilters,
    );
  });

  it("ignores a cancelled search error even before the replacement debounce starts", async () => {
    let rejectOld!: (error: unknown) => void;
    queryResultsMock
      .mockResolvedValueOnce(page([item("artifact:current")]))
      .mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectOld = reject; }));
    const hook = renderHook(({ query }) => useResultsQuery({
      enabled: true, query, scope: "session", sessionId: "s_1",
    }), { initialProps: { query: "" } });
    await flushLoad();
    hook.rerender({ query: "old" });
    await flushLoad();
    hook.rerender({ query: "new" });
    await act(async () => { rejectOld(new ResultsApiError("results_scope_unavailable")); });
    expect(hook.result.current.unavailable).toBe(false);
    expect(hook.result.current.error).toBeNull();
    expect(hook.result.current.items[0]?.key).toBe("artifact:current");
    expect(hook.result.current.loading).toBe(true);
  });

  it("does not let an old changes response publish into a newly accepted snapshot", async () => {
    let resolveChanges!: (value: ResultsQueryResponse) => void;
    queryResultsMock
      .mockResolvedValueOnce(page([item("artifact:old")]))
      .mockImplementationOnce(() => new Promise((resolve) => { resolveChanges = resolve; }))
      .mockResolvedValueOnce(page([item("artifact:new")], { snapshot_token: "fresh" }));
    const hook = renderHook(() => useResultsQuery({
      enabled: true, query: "", scope: "session", sessionId: "s_1",
    }));
    await flushLoad();
    let checking!: Promise<void>;
    act(() => { checking = hook.result.current.checkChanges(); });
    await act(async () => { await hook.result.current.acceptChanges(); });
    await act(async () => {
      resolveChanges(page([], { new_results: { count: 7, projection_changed: true } }));
      await checking;
    });
    expect(hook.result.current.newResultsCount).toBeNull();
    expect(hook.result.current.projectionChanged).toBe(false);
    expect(hook.result.current.items[0]?.key).toBe("artifact:new");
  });

  it("ignores locate failures from a scope that is no longer open", async () => {
    let rejectLocate!: (error: unknown) => void;
    queryResultsMock
      .mockResolvedValueOnce(page([item("artifact:old")]))
      .mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectLocate = reject; }))
      .mockResolvedValueOnce(page([item("artifact:new")]));
    const hook = renderHook(({ sessionId }) => useResultsQuery({
      enabled: true, query: "", scope: "session", sessionId,
    }), { initialProps: { sessionId: "s_old" } });
    await flushLoad();
    let locating!: Promise<ResultItemDTO | null>;
    act(() => { locating = hook.result.current.locate("artifact:deep"); });
    hook.rerender({ sessionId: "s_new" });
    await flushLoad();
    await act(async () => {
      rejectLocate(new ResultsApiError("results_scope_unavailable"));
      await locating;
    });
    expect(hook.result.current.unavailable).toBe(false);
    expect(hook.result.current.items[0]?.key).toBe("artifact:new");
  });

  it("locates a visible page and prepends its independent previous page", async () => {
    queryResultsMock
      .mockResolvedValueOnce(page([item("artifact:first")]))
      .mockResolvedValueOnce(page([item("artifact:deep", 101)], {
        authorized_total: 150,
        matched_total: 150,
        page_start: 100,
        previous_cursor: "previous-100",
        next_cursor: "next-150",
        has_more: true,
      }))
      .mockResolvedValueOnce(page([item("artifact:before", 51)], {
        authorized_total: 150,
        matched_total: 150,
        page_start: 50,
        previous_cursor: "previous-50",
        next_cursor: "next-100",
        has_more: true,
      }));
    const hook = renderHook(() => useResultsQuery({
      enabled: true,
      query: "",
      scope: "session",
      sessionId: "s_1",
    }));
    await flushLoad();

    await act(async () => {
      await hook.result.current.locatePage("artifact:deep");
    });
    expect(hook.result.current.items.map((value) => value.key)).toEqual([
      "artifact:deep",
    ]);
    expect(hook.result.current.pageStart).toBe(100);
    expect(hook.result.current.hasPrevious).toBe(true);

    await act(async () => {
      await hook.result.current.loadPrevious();
    });
    expect(hook.result.current.items.map((value) => value.key)).toEqual([
      "artifact:before",
      "artifact:deep",
    ]);
    expect(queryResultsMock.mock.calls[2]?.[0].request).toMatchObject({
      surface: "explorer",
      cursor: "previous-100",
      snapshot_token: "snapshot",
    });
  });

  it.each([
    { position: 14, pageStart: 0 },
    { position: 101, pageStart: 100 },
  ])("restores initial selection at position $position with one page-producing locate", async ({
    pageStart,
    position,
  }) => {
    queryResultsMock.mockResolvedValueOnce(page([item("artifact:deep", position)], {
      authorized_total: 137,
      matched_total: 137,
      page_start: pageStart,
      previous_cursor: pageStart > 0 ? `previous-${pageStart}` : null,
    }));
    const hook = renderHook(() => useResultsQuery({
      enabled: true,
      query: "",
      scope: "session",
      sessionId: "s_1",
      selectionKey: "artifact:deep",
    }));

    await flushLoad();

    expect(queryResultsMock).toHaveBeenCalledTimes(1);
    expect(queryResultsMock.mock.calls[0]?.[0].request).toMatchObject({
      surface: "locate",
      anchor_key: "artifact:deep",
      page_size: 50,
    });
    expect(hook.result.current.items.map((value) => value.key)).toEqual([
      "artifact:deep",
    ]);
    expect(hook.result.current.catalogItems.map((value) => value.key)).toEqual([
      "artifact:deep",
    ]);
    expect(hook.result.current.pageStart).toBe(pageStart);
  });

  it("clears a failed locate when its fallback page already contains the selected object", async () => {
    queryResultsMock
      .mockRejectedValueOnce(new ResultsApiError("results_anchor_not_in_view", 409))
      .mockResolvedValueOnce(page([item("artifact:found")]));
    const hook = renderHook(() => useResultsQuery({
      enabled: true, query: "", scope: "session", sessionId: "s_1", selectionKey: "artifact:found",
    }));
    await flushLoad();
    expect(hook.result.current.items.map((value) => value.key)).toEqual(["artifact:found"]);
    expect(hook.result.current.error).toBeNull();
    expect(hook.result.current.errorAnchorKey).toBeNull();
  });

  it("keeps the fallback load failure distinct from the earlier locate failure", async () => {
    queryResultsMock
      .mockRejectedValueOnce(new ResultsApiError("results_anchor_not_in_view", 409))
      .mockRejectedValueOnce(new ResultsApiError("results_request_failed", 503));
    const hook = renderHook(() => useResultsQuery({
      enabled: true, query: "", scope: "session", sessionId: "s_1", selectionKey: "artifact:missing",
    }));
    await flushLoad();
    expect(hook.result.current.error).toBe("results_request_failed");
    expect(hook.result.current.errorAnchorKey).toBeNull();
  });

  it("falls back to one browsable base page when the initial selection cannot be located", async () => {
    queryResultsMock
      .mockRejectedValueOnce(new ResultsApiError("results_anchor_not_in_view", 409))
      .mockResolvedValueOnce(page([item("artifact:first")], {
        authorized_total: 137,
        matched_total: 137,
      }));
    const hook = renderHook(() => useResultsQuery({
      enabled: true,
      query: "",
      scope: "session",
      sessionId: "s_1",
      selectionKey: "artifact:missing",
    }));

    await flushLoad();

    expect(queryResultsMock).toHaveBeenCalledTimes(2);
    expect(queryResultsMock.mock.calls.map(([{ request }]) => request.surface)).toEqual([
      "locate",
      "explorer",
    ]);
    expect(hook.result.current.items.map((value) => value.key)).toEqual([
      "artifact:first",
    ]);
    expect(hook.result.current.error).toBe("results_anchor_not_in_view");
    expect(hook.result.current.errorAnchorKey).toBe("artifact:missing");
  });

  it("commits target conditions, rows and counts together and does not debounce a facet click", async () => {
    let resolveFilter!: (value: ResultsQueryResponse) => void;
    queryResultsMock
      .mockResolvedValueOnce(page([item("artifact:before")]))
      .mockImplementationOnce(() => new Promise((resolve) => { resolveFilter = resolve; }));
    const all: ResultsFiltersDTO = { lens: "all", primary: "all", secondary: "all", technical_only: false };
    const input: ResultsFiltersDTO = { ...all, primary: "evidence", secondary: "input" };
    const hook = renderHook(({ filters }) => useResultsQuery({
      enabled: true, query: "csv", filters, scope: "session", sessionId: "s_1",
    }), { initialProps: { filters: all } });
    await flushLoad();
    hook.rerender({ filters: input });
    await act(async () => { vi.advanceTimersByTime(0); });
    expect(queryResultsMock).toHaveBeenCalledTimes(2);
    expect(hook.result.current.completedView).toEqual({ query: "csv", filters: all });
    expect(hook.result.current.matchedTotal).toBe(1);
    await act(async () => {
      resolveFilter(page([item("artifact:after")], { matched_total: 12, authorized_total: 15 }));
    });
    expect(hook.result.current.completedView).toEqual({ query: "csv", filters: input });
    expect(hook.result.current.items[0]?.key).toBe("artifact:after");
    expect(hook.result.current.matchedTotal).toBe(12);
    expect(hook.result.current.authorizedTotal).toBe(15);
    expect(hook.result.current.loading).toBe(false);
  });

  it.each(["loadNext", "loadPrevious", "locatePage"] as const)(
    "ignores a cancelled %s failure after filters have committed", async (operation) => {
      let rejectOld!: (error: unknown) => void;
      queryResultsMock
        .mockResolvedValueOnce(page([item("artifact:old")], { next_cursor: "next", previous_cursor: "prev" }))
        .mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectOld = reject; }))
        .mockResolvedValueOnce(page([item("artifact:new")]));
      const hook = renderHook(({ query }) => useResultsQuery({
        enabled: true, query, scope: "session", sessionId: "s_1",
      }), { initialProps: { query: "" } });
      await flushLoad();
      let pending!: Promise<unknown>;
      act(() => { pending = hook.result.current[operation]("artifact:deep"); });
      hook.rerender({ query: "new" });
      await flushLoad();
      await act(async () => {
        rejectOld(new ResultsApiError("results_scope_unavailable"));
        await pending;
      });
      expect(hook.result.current.error).toBeNull();
      expect(hook.result.current.unavailable).toBe(false);
      expect(hook.result.current.items[0]?.key).toBe("artifact:new");
    },
  );

  it("fences A → B → A when a cancelled A succeeds before the new A starts", async () => {
    let resolveOld!: (value: ResultsQueryResponse) => void;
    queryResultsMock.mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve; }))
      .mockResolvedValueOnce(page([item("artifact:fresh")]));
    const hook = renderHook(({ query }) => useResultsQuery({
      enabled: true, query, scope: "session", sessionId: "s_1",
    }), { initialProps: { query: "A" } });
    await flushLoad();
    hook.rerender({ query: "B" });
    hook.rerender({ query: "A" });
    await act(async () => { resolveOld(page([item("artifact:stale")])); });
    expect(hook.result.current.items).toEqual([]);
    expect(hook.result.current.loading).toBe(true);
    await flushLoad();
    expect(hook.result.current.items[0]?.key).toBe("artifact:fresh");
  });

  it.each(["success", "error"])("ignores a locate %s after the user selects another object", async (outcome) => {
    let resolveOld!: (value: ResultsQueryResponse) => void;
    let rejectOld!: (error: unknown) => void;
    queryResultsMock.mockResolvedValueOnce(page([item("artifact:first")]))
      .mockImplementationOnce(() => new Promise((resolve, reject) => { resolveOld = resolve; rejectOld = reject; }));
    const hook = renderHook(({ selectionKey }) => useResultsQuery({
      enabled: true, query: "", scope: "session", sessionId: "s_1", selectionKey,
    }), { initialProps: { selectionKey: "artifact:old-selection" } });
    await flushLoad();
    let pending!: Promise<unknown>;
    act(() => { pending = hook.result.current.locatePage("artifact:old-selection"); });
    hook.rerender({ selectionKey: "artifact:first" });
    await act(async () => {
      if (outcome === "success") resolveOld(page([item("artifact:old-selection")]));
      else rejectOld(new ResultsApiError("results_anchor_not_in_view"));
      await pending;
    });
    expect(hook.result.current.error).toBeNull();
    expect(hook.result.current.loading).toBe(false);
    expect(hook.result.current.items[0]?.key).toBe("artifact:first");
  });

  it("discards requests from a previous history entry with the same scope and query", async () => {
    let resolveOld!: (value: ResultsQueryResponse) => void;
    queryResultsMock.mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve; }))
      .mockResolvedValueOnce(page([item("artifact:new-entry")]));
    const hook = renderHook(({ entryId }) => useResultsQuery({
      enabled: true, query: "", scope: "session", sessionId: "s_1", entryId,
    }), { initialProps: { entryId: "entry-1" } });
    await flushLoad();
    hook.rerender({ entryId: "entry-2" });
    await flushLoad();
    await act(async () => { resolveOld(page([item("artifact:old-entry")])); });
    expect(hook.result.current.items[0]?.key).toBe("artifact:new-entry");
    expect(hook.result.current.catalogItems.map((value) => value.key)).toEqual(["artifact:new-entry"]);
  });

  it("checks for changes without replacing items and accepts a new snapshot explicitly", async () => {
    queryResultsMock
      .mockResolvedValueOnce(page([item("artifact:old")]))
      .mockResolvedValueOnce(page([], {
        new_results: { count: 3, projection_changed: true },
      }))
      .mockResolvedValueOnce(page([item("artifact:fresh")], {
        snapshot_token: "snapshot-new",
      }));
    const hook = renderHook(() => useResultsQuery({
      enabled: true,
      query: "",
      scope: "session",
      sessionId: "s_1",
    }));
    await flushLoad();

    await act(async () => {
      await hook.result.current.checkChanges();
    });
    expect(hook.result.current.items[0]?.key).toBe("artifact:old");
    expect(hook.result.current.newResultsCount).toBe(3);
    expect(hook.result.current.projectionChanged).toBe(true);
    expect(queryResultsMock.mock.calls[1]?.[0].request).toMatchObject({
      surface: "changes",
      snapshot_token: "snapshot",
    });

    await act(async () => {
      await hook.result.current.acceptChanges();
    });
    expect(hook.result.current.items[0]?.key).toBe("artifact:fresh");
    expect(hook.result.current.newResultsCount).toBeNull();
    expect(hook.result.current.projectionChanged).toBe(false);
    expect(queryResultsMock.mock.calls[2]?.[0].request).not.toHaveProperty(
      "snapshot_token",
    );
  });

  it("allows honest legacy fallback only for an initial read-model failure", async () => {
    queryResultsMock.mockRejectedValue(
      new ResultsApiError("results_read_model_unavailable", 503),
    );
    const hook = renderHook(() => useResultsQuery({
      enabled: true,
      query: "",
      scope: "session",
      sessionId: "s_1",
    }));
    await flushLoad();

    expect(hook.result.current.fallbackAllowed).toBe(true);
    expect(hook.result.current.unavailable).toBe(false);
  });

  it("does not turn a completed empty search into an initial legacy fallback on failure", async () => {
    queryResultsMock.mockResolvedValueOnce(page([]))
      .mockRejectedValueOnce(new ResultsApiError("results_read_model_unavailable", 503));
    const hook = renderHook(({ query }) => useResultsQuery({
      enabled: true, query, scope: "session", sessionId: "s_1",
    }), { initialProps: { query: "empty" } });
    await flushLoad();
    hook.rerender({ query: "next" });
    await flushLoad();
    expect(hook.result.current.fallbackAllowed).toBe(false);
    expect(hook.result.current.completedView?.query).toBe("empty");
    expect(hook.result.current.error).toBe("results_read_model_unavailable");
  });

  it("keeps an independent selected-object catalog read alive during a filter change", async () => {
    let resolveLocate!: (value: ResultsQueryResponse) => void;
    queryResultsMock.mockResolvedValueOnce(page([item("artifact:first")]))
      .mockImplementationOnce(() => new Promise((resolve) => { resolveLocate = resolve; }))
      .mockResolvedValueOnce(page([item("artifact:search")]));
    const hook = renderHook(({ query }) => useResultsQuery({
      enabled: true, query, scope: "session", sessionId: "s_1",
    }), { initialProps: { query: "" } });
    await flushLoad();
    let locating!: Promise<ResultItemDTO | null>;
    act(() => { locating = hook.result.current.locate("artifact:selected"); });
    hook.rerender({ query: "search" });
    await flushLoad();
    await act(async () => {
      resolveLocate(page([item("artifact:selected")]));
      expect((await locating)?.key).toBe("artifact:selected");
    });
    expect(hook.result.current.items[0]?.key).toBe("artifact:search");
    expect(hook.result.current.catalogItems.some((value) => value.key === "artifact:selected")).toBe(true);
  });

  it("keeps page loading independent when changes finishes, then preserves its notification on page commit", async () => {
    let resolveChanges!: (value: ResultsQueryResponse) => void;
    let resolveNext!: (value: ResultsQueryResponse) => void;
    queryResultsMock.mockResolvedValueOnce(page([item("artifact:first")], { next_cursor: "next" }))
      .mockImplementationOnce(() => new Promise((resolve) => { resolveChanges = resolve; }))
      .mockImplementationOnce(() => new Promise((resolve) => { resolveNext = resolve; }));
    const hook = renderHook(() => useResultsQuery({ enabled: true, query: "", scope: "session", sessionId: "s_1" }));
    await flushLoad();
    let checking!: Promise<void>;
    let paging!: Promise<void>;
    act(() => { checking = hook.result.current.checkChanges(); });
    act(() => { paging = hook.result.current.loadNext(); });
    await act(async () => {
      resolveChanges(page([], { new_results: { count: 3, projection_changed: false } }));
      await checking;
    });
    expect(hook.result.current.loadingMore).toBe(true);
    expect(hook.result.current.items).toHaveLength(1);
    await act(async () => {
      resolveNext(page([item("artifact:second")]));
      await paging;
    });
    expect(hook.result.current.newResultsCount).toBe(3);
    expect(hook.result.current.items).toHaveLength(2);
  });

  it("does not reuse a stale page cursor when retrying", async () => {
    queryResultsMock
      .mockResolvedValueOnce(page([item("artifact:old")], {
        authorized_total: 2,
        matched_total: 2,
        next_cursor: "stale-next",
        has_more: true,
      }))
      .mockRejectedValueOnce(new ResultsApiError("results_snapshot_stale", 409))
      .mockResolvedValueOnce(page([item("artifact:fresh")], {
        authorized_total: 1,
        matched_total: 1,
      }));
    const hook = renderHook(() => useResultsQuery({
      enabled: true,
      query: "",
      scope: "session",
      sessionId: "s_1",
    }));
    await flushLoad();

    await act(async () => {
      await hook.result.current.loadMore();
    });
    expect(hook.result.current.items[0]?.key).toBe("artifact:old");
    expect(hook.result.current.error).toBe("results_snapshot_stale");

    await act(async () => {
      await hook.result.current.retry();
    });
    expect(queryResultsMock.mock.calls[2]?.[0].request).not.toHaveProperty("cursor");
    expect(queryResultsMock.mock.calls[2]?.[0].request).not.toHaveProperty("snapshot_token");
    expect(hook.result.current.items[0]?.key).toBe("artifact:fresh");
  });

  it("locates a deep item into the catalog without replacing the visible page", async () => {
    queryResultsMock
      .mockResolvedValueOnce(page([item("artifact:first")]))
      .mockResolvedValueOnce(page([item("artifact:deep", 101)]));
    const hook = renderHook(() => useResultsQuery({
      enabled: true,
      query: "",
      scope: "report",
      sessionId: "s_report",
    }));
    await flushLoad();
    expect(hook.result.current.items).toHaveLength(1);

    await act(async () => {
      await hook.result.current.locate("artifact:deep");
    });

    expect(hook.result.current.items.map((value) => value.key)).toEqual([
      "artifact:first",
    ]);
    expect(hook.result.current.catalogItems.map((value) => value.key)).toEqual([
      "artifact:first",
      "artifact:deep",
    ]);
  });

  it("purges authorized metadata when report scope becomes unavailable", async () => {
    queryResultsMock
      .mockResolvedValueOnce(page([item("artifact:first")]))
      .mockRejectedValueOnce(new ResultsApiError("results_scope_unavailable", 404));
    const hook = renderHook(() => useResultsQuery({
      enabled: true,
      query: "",
      scope: "report",
      sessionId: "s_report",
    }));
    await flushLoad();
    expect(hook.result.current.catalogItems).toHaveLength(1);

    await act(async () => {
      await hook.result.current.locate("artifact:revoked");
    });

    expect(hook.result.current.catalogItems).toEqual([]);
    expect(hook.result.current.unavailable).toBe(true);
    expect(hook.result.current.fallbackAllowed).toBe(false);
  });

  it("clears the scope catalog when Complete is disabled", async () => {
    queryResultsMock.mockResolvedValue(page([item("artifact:first")]));
    const hook = renderHook(
      ({ enabled }) => useResultsQuery({
        enabled,
        query: "",
        scope: "session",
        sessionId: "s_1",
      }),
      { initialProps: { enabled: true } },
    );
    await flushLoad();
    expect(hook.result.current.catalogItems).toHaveLength(1);

    hook.rerender({ enabled: false });

    expect(hook.result.current.catalogItems).toEqual([]);
  });
});
