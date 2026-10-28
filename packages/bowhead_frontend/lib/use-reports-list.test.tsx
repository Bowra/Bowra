import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { ReportListItem, ReportListPage, ReportListQuery } from "./api";
import { mergeReportPages, useReportsList } from "./use-reports-list";

function item(id: string): ReportListItem {
  return {
    id,
    session_id: `s_${id}`,
    workspace_id: "ws_1",
    title: id,
    status: "active",
    created_at: "2026-10-01T00:00:00Z",
    updated_at: "2026-10-01T00:00:00Z",
    sections_count: 1,
  };
}

function page(
  ids: string[],
  total: number | null,
  next: string | null,
): ReportListPage {
  return {
    items: ids.map(item),
    total,
    next_cursor: next,
    has_more: next !== null,
  };
}

type PageArgs = ReportListQuery & { cursor?: string | null };

describe("mergeReportPages", () => {
  it("appends new rows and keeps the first occurrence of a repeated id", () => {
    const merged = mergeReportPages(
      [item("a"), item("b")],
      [item("b"), item("c")],
    );
    expect(merged.map((r) => r.id)).toEqual(["a", "b", "c"]);
  });
});

describe("useReportsList", () => {
  it("loads the first page with the true total and appends the next page", async () => {
    const fetchPage = vi.fn(async (opts: PageArgs) =>
      opts.cursor === "c1"
        ? page(["c", "b2"], 4, null)
        : page(["a", "b"], 4, "c1"),
    );
    const { result } = renderHook(() =>
      useReportsList({ sort: "updated_desc", limit: 2 }, fetchPage),
    );

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.items.map((r) => r.id)).toEqual(["a", "b"]);
    expect(result.current.total).toBe(4);
    expect(result.current.hasMore).toBe(true);

    await act(async () => {
      await result.current.loadMore();
    });

    expect(fetchPage).toHaveBeenLastCalledWith({
      sort: "updated_desc",
      limit: 2,
      cursor: "c1",
    });
    expect(result.current.items.map((r) => r.id)).toEqual([
      "a",
      "b",
      "c",
      "b2",
    ]);
    expect(result.current.hasMore).toBe(false);
  });

  it("dedupes a row that moved between pages", async () => {
    const fetchPage = vi.fn(async (opts: PageArgs) =>
      opts.cursor ? page(["b", "c"], 3, null) : page(["a", "b"], 3, "next"),
    );
    const { result } = renderHook(() => useReportsList({}, fetchPage));
    await waitFor(() => expect(result.current.loading).toBe(false));

    await act(async () => {
      await result.current.loadMore();
    });

    expect(result.current.items.map((r) => r.id)).toEqual(["a", "b", "c"]);
  });

  it("does not request the next page twice while one is in flight", async () => {
    let release: (value: ReportListPage) => void = () => {};
    const fetchPage = vi.fn((opts: PageArgs) =>
      opts.cursor
        ? new Promise<ReportListPage>((resolve) => {
            release = resolve;
          })
        : Promise.resolve(page(["a"], 2, "next")),
    );
    const { result } = renderHook(() => useReportsList({}, fetchPage));
    await waitFor(() => expect(result.current.loading).toBe(false));

    let first: Promise<void> = Promise.resolve();
    act(() => {
      first = result.current.loadMore();
      void result.current.loadMore();
    });
    expect(fetchPage).toHaveBeenCalledTimes(2);
    await act(async () => {
      release(page(["b"], 2, null));
      await first;
    });
    expect(result.current.items.map((r) => r.id)).toEqual(["a", "b"]);
  });

  it("restarts from the first page when the query changes and drops stale pages", async () => {
    let releaseMore: (value: ReportListPage) => void = () => {};
    const fetchPage = vi.fn((opts: PageArgs) => {
      if (opts.cursor)
        return new Promise<ReportListPage>((resolve) => {
          releaseMore = resolve;
        });
      return Promise.resolve(
        opts.q === "gmv" ? page(["g1"], 1, null) : page(["a"], 5, "next"),
      );
    });
    const { result, rerender } = renderHook(
      ({ q }: { q?: string }) => useReportsList({ q }, fetchPage),
      { initialProps: {} as { q?: string } },
    );
    await waitFor(() => expect(result.current.loading).toBe(false));

    let more: Promise<void> = Promise.resolve();
    act(() => {
      more = result.current.loadMore();
    });
    rerender({ q: "gmv" });
    await waitFor(() =>
      expect(result.current.items.map((r) => r.id)).toEqual(["g1"]),
    );

    await act(async () => {
      releaseMore(page(["stale"], 5, null));
      await more;
    });
    expect(result.current.items.map((r) => r.id)).toEqual(["g1"]);
    expect(result.current.total).toBe(1);
  });

  it("keeps the first-page total when cursor pages return total=null", async () => {
    const fetchPage = vi.fn(async (opts: PageArgs) =>
      opts.cursor ? page(["b"], null, null) : page(["a"], 2, "next"),
    );
    const { result } = renderHook(() => useReportsList({}, fetchPage));
    await waitFor(() => expect(result.current.loading).toBe(false));

    await act(async () => {
      await result.current.loadMore();
    });

    expect(result.current.items.map((r) => r.id)).toEqual(["a", "b"]);
    expect(result.current.total).toBe(2);
  });

  it("keeps loaded rows visible when loading more fails, and can retry", async () => {
    let failNext = true;
    const fetchPage = vi.fn(async (opts: PageArgs) => {
      if (!opts.cursor) return page(["a"], 2, "next");
      if (failNext) {
        failNext = false;
        throw new Error("boom");
      }
      return page(["b"], null, null);
    });
    const { result } = renderHook(() => useReportsList({}, fetchPage));
    await waitFor(() => expect(result.current.loading).toBe(false));

    await act(async () => {
      await result.current.loadMore();
    });
    expect(result.current.items.map((r) => r.id)).toEqual(["a"]);
    expect(result.current.error).toBeNull();
    expect(result.current.loadMoreError).toBeInstanceOf(Error);
    expect(result.current.hasMore).toBe(true);

    await act(async () => {
      await result.current.loadMore();
    });
    expect(result.current.items.map((r) => r.id)).toEqual(["a", "b"]);
    expect(result.current.loadMoreError).toBeNull();
  });

  it("lets the new query page while an old load-more is still pending", async () => {
    const fetchPage = vi.fn((opts: PageArgs) => {
      if (opts.cursor === "old") return new Promise<ReportListPage>(() => {});
      if (opts.cursor === "new")
        return Promise.resolve(page(["n2"], null, null));
      return Promise.resolve(
        opts.q === "gmv" ? page(["n1"], 2, "new") : page(["a"], 5, "old"),
      );
    });
    const { result, rerender } = renderHook(
      ({ q }: { q?: string }) => useReportsList({ q }, fetchPage),
      { initialProps: {} as { q?: string } },
    );
    await waitFor(() => expect(result.current.loading).toBe(false));
    act(() => {
      void result.current.loadMore();
    });

    rerender({ q: "gmv" });
    await waitFor(() =>
      expect(result.current.items.map((r) => r.id)).toEqual(["n1"]),
    );
    await act(async () => {
      await result.current.loadMore();
    });

    expect(fetchPage).toHaveBeenLastCalledWith({ q: "gmv", cursor: "new" });
    expect(result.current.items.map((r) => r.id)).toEqual(["n1", "n2"]);
  });

  it("a silent reload replaces the old first batch, keeps later batches and their cursor", async () => {
    let first = page(["a", "b"], 4, "c1");
    const fetchPage = vi.fn(async (opts: PageArgs) =>
      opts.cursor === "c1" ? page(["c", "d"], null, "c2") : first,
    );
    const { result } = renderHook(() => useReportsList({ limit: 2 }, fetchPage));
    await waitFor(() => expect(result.current.loading).toBe(false));
    await act(async () => {
      await result.current.loadMore();
    });

    first = page(["new", "a"], 5, "x1");
    await act(async () => {
      await result.current.reload({ silent: true });
    });
    // "b" left the first page (deleted or filtered out) and is dropped; later batches stay.
    expect(result.current.items.map((r) => r.id)).toEqual(["new", "a", "c", "d"]);
    expect(result.current.total).toBe(5);
    expect(result.current.items.length).toBeLessThanOrEqual(result.current.total);
    expect(result.current.nextCursor).toBe("c2");
  });

  it("removeItem drops a deleted row and lowers the total by one", async () => {
    const fetchPage = vi.fn(async () => page(["a", "b"], 7, "c1"));
    const { result } = renderHook(() => useReportsList({ limit: 2 }, fetchPage));
    await waitFor(() => expect(result.current.loading).toBe(false));
    act(() => result.current.removeItem("a"));
    expect(result.current.items.map((r) => r.id)).toEqual(["b"]);
    expect(result.current.total).toBe(6);
    act(() => result.current.removeItem("missing"));
    expect(result.current.total).toBe(6);
  });

  it("a silent reload does not discard an in-flight load more", async () => {
    let releaseMore: (value: ReportListPage) => void = () => {};
    const fetchPage = vi.fn((opts: PageArgs) =>
      opts.cursor === "c1"
        ? new Promise<ReportListPage>((resolve) => { releaseMore = resolve; })
        : Promise.resolve(page(["a", "b"], 4, "c1")),
    );
    const { result } = renderHook(() => useReportsList({ limit: 2 }, fetchPage));
    await waitFor(() => expect(result.current.loading).toBe(false));
    let more: Promise<void> = Promise.resolve();
    act(() => {
      more = result.current.loadMore();
    });
    expect(result.current.loadingMore).toBe(true);
    await act(async () => {
      await result.current.reload({ silent: true });
    });
    expect(result.current.loadingMore).toBe(true);
    await act(async () => {
      releaseMore(page(["c", "d"], null, null));
      await more;
    });
    expect(result.current.items.map((r) => r.id)).toEqual(["a", "b", "c", "d"]);
    expect(result.current.loadingMore).toBe(false);
    expect(result.current.hasMore).toBe(false);
  });

  it("keeps the first page's empty_total and ignores a failed silent reload", async () => {
    let fail = false;
    const fetchPage = vi.fn(async () => {
      if (fail) throw new Error("offline");
      return { ...page(["a"], 1, null), empty_total: 3 };
    });
    const { result } = renderHook(() => useReportsList({ include_empty: false }, fetchPage));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.emptyTotal).toBe(3);
    fail = true;
    await act(async () => {
      await result.current.reload({ silent: true });
    });
    expect(result.current.error).toBeNull();
    expect(result.current.items.map((r) => r.id)).toEqual(["a"]);
  });
});
