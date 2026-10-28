import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  fetchUnifiedWorkSearch,
  searchResultCount,
  unifiedWorkResultHref,
  useUnifiedWorkSearch,
} from "./unified-work-search";
import { reportDisplayTitle } from "./report-title";

describe("unified work search", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn());
    const store = new Map<string, string>();
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      value: {
        getItem: (key: string) => store.get(key) ?? null,
        setItem: (key: string, value: string) => store.set(key, String(value)),
        removeItem: (key: string) => store.delete(key),
        clear: () => store.clear(),
      },
    });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    delete window.__BOWHEAD_BASE_PATH__;
  });

  it("honors runtime base path and active space headers", async () => {
    vi.useRealTimers();
    window.__BOWHEAD_BASE_PATH__ = "/bowhead";
    window.localStorage.setItem("bowhead.space_id", "ws_1");
    vi.mocked(fetch).mockResolvedValue(
      new Response(
        JSON.stringify({
          query: "收入",
          partial: false,
          groups: [
            { kind: "session", unavailable: false, items: [] },
            { kind: "report", unavailable: false, items: [] },
            { kind: "file", unavailable: false, items: [] },
            { kind: "knowledge", unavailable: false, items: [] },
          ],
        }),
        { status: 200 },
      ),
    );

    const response = await fetchUnifiedWorkSearch("收入");

    expect(response.groups).toHaveLength(4);
    expect(response.partial).toBe(false);
    const [url, options] = vi.mocked(fetch).mock.calls[0] ?? [];
    expect(url).toBe("/bowhead/api/v1/search");
    expect(options?.method).toBe("POST");
    expect(JSON.parse(String(options?.body))).toEqual({
      query: "收入",
      limit: 5,
    });
    expect(new Headers(options?.headers).get("X-Space-Id")).toBe("ws_1");
  });

  it("titles untitled reports like the report list, never with the canvas id", async () => {
    vi.useRealTimers();
    const report = (id: string, title: string, createdAt?: string) => ({
      kind: "report",
      id,
      title,
      session_id: `s_${id}`,
      updated_at: "2026-10-06T08:00:00Z",
      ...(createdAt ? { created_at: createdAt } : {}),
    });
    vi.mocked(fetch).mockResolvedValue(
      new Response(
        JSON.stringify({
          query: "report",
          partial: false,
          groups: [
            { kind: "session", unavailable: false, items: [] },
            {
              kind: "report",
              unavailable: false,
              items: [
                report("cv_796810b91267", "", "2026-10-06T07:38:00Z"),
                report("cv_35847490c01d", ""),
                report("cv_named", "  Revenue by region  "),
              ],
            },
            { kind: "file", unavailable: false, items: [] },
            { kind: "knowledge", unavailable: false, items: [] },
          ],
        }),
        { status: 200 },
      ),
    );

    const response = await fetchUnifiedWorkSearch("report");

    const titles = response.groups[1]?.items.map((item) => item.title);
    expect(titles).toEqual([
      reportDisplayTitle({
        id: "cv_796810b91267",
        title: null,
        first_section_title: null,
        created_at: "2026-10-06T07:38:00Z",
      }),
      // An older server without created_at: fall back to updated_at, still no id.
      reportDisplayTitle({
        id: "cv_35847490c01d",
        title: null,
        first_section_title: null,
        created_at: "2026-10-06T08:00:00Z",
      }),
      "Revenue by region",
    ]);
    expect(titles?.[0]).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2} /);
    for (const title of titles ?? []) expect(title).not.toContain("cv_");
  });

  it("keeps a timed-out source distinct from an empty one", async () => {
    vi.useRealTimers();
    vi.mocked(fetch).mockResolvedValue(
      new Response(
        JSON.stringify({
          query: "收入",
          partial: true,
          groups: [
            { kind: "session", unavailable: true, timed_out: true, items: [] },
            { kind: "report", unavailable: false, items: [] },
            { kind: "file", unavailable: true, items: [] },
            { kind: "knowledge", unavailable: false, items: [] },
          ],
        }),
        { status: 200 },
      ),
    );

    const response = await fetchUnifiedWorkSearch("收入");

    expect(response.groups.map((group) => group.timedOut)).toEqual([
      true,
      false,
      false,
      false,
    ]);
    expect(response.partial).toBe(true);
  });

  it("does not call the API before two characters", () => {
    const { result } = renderHook(() => useUnifiedWorkSearch("收"));

    expect(result.current.status).toBe("typing");
    expect(fetch).not.toHaveBeenCalled();
  });

  it("rejects a response that does not echo the current normalized query", async () => {
    vi.useRealTimers();
    vi.mocked(fetch).mockResolvedValue(
      new Response(
        JSON.stringify({ query: "other", partial: false, groups: [] }),
        { status: 200 },
      ),
    );

    await expect(fetchUnifiedWorkSearch("收入")).rejects.toThrow(
      "stale search response",
    );
  });

  it("rejects a nominally successful response when every source is unavailable", async () => {
    vi.useRealTimers();
    vi.mocked(fetch).mockResolvedValue(
      new Response(
        JSON.stringify({ query: "收入", partial: true, groups: [] }),
        { status: 200 },
      ),
    );

    await expect(fetchUnifiedWorkSearch("收入")).rejects.toThrow(
      "unified search unavailable",
    );
  });

  it("debounces and aborts the stale request when the query changes", async () => {
    const signals: AbortSignal[] = [];
    vi.mocked(fetch).mockImplementation((_url, options) => {
      signals.push(options?.signal as AbortSignal);
      return new Promise<Response>(() => {});
    });
    const { rerender } = renderHook(
      ({ query }) => useUnifiedWorkSearch(query),
      { initialProps: { query: "收入" } },
    );

    await act(async () => vi.advanceTimersByTime(250));
    expect(fetch).toHaveBeenCalledTimes(1);
    rerender({ query: "收入趋势" });
    expect(signals[0]?.aborted).toBe(true);
    await act(async () => vi.advanceTimersByTime(250));
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it("exposes a retry after a recoverable request failure", async () => {
    vi.mocked(fetch)
      .mockResolvedValueOnce(new Response("no", { status: 503 }))
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            query: "收入",
            partial: false,
            groups: [
              { kind: "session", unavailable: false, items: [] },
              { kind: "report", unavailable: false, items: [] },
              { kind: "file", unavailable: false, items: [] },
              { kind: "knowledge", unavailable: false, items: [] },
            ],
          }),
          { status: 200 },
        ),
      );
    const { result } = renderHook(() => useUnifiedWorkSearch("收入"));

    await act(async () => vi.advanceTimersByTimeAsync(250));
    expect(result.current.status).toBe("error");
    act(() => result.current.retry());
    await act(async () => vi.advanceTimersByTimeAsync(250));
    expect(result.current.status).toBe("ready");
  });

  it("turns a hung request into a recoverable error", async () => {
    vi.mocked(fetch).mockImplementation(() => new Promise<Response>(() => {}));
    const { result } = renderHook(() => useUnifiedWorkSearch("收入"));

    await act(async () => vi.advanceTimersByTimeAsync(7_250));

    expect(result.current.status).toBe("error");
  });

  it("pages the session group from the first response's cursor and keeps it for a retry", async () => {
    const sessionItem = (id: string) => ({
      kind: "session",
      id,
      title: `复购 ${id}`,
      session_id: id,
      snippet: null,
      updated_at: null,
      match: "title",
    });
    const first = {
      query: "复购",
      partial: false,
      groups: [
        {
          kind: "session",
          unavailable: false,
          items: ["s1", "s2"].map(sessionItem),
          total: 5,
          next_cursor: "c1",
        },
        { kind: "report", unavailable: false, items: [] },
        { kind: "file", unavailable: false, items: [] },
        { kind: "knowledge", unavailable: false, items: [] },
      ],
    };
    const second = {
      query: "复购",
      partial: false,
      groups: [
        {
          kind: "session",
          unavailable: false,
          // s2 repeats (a session became active between pages): shown once.
          items: ["s2", "s3", "s4"].map(sessionItem),
          total: 5,
          next_cursor: "c2",
        },
      ],
    };
    vi.mocked(fetch)
      .mockResolvedValueOnce(new Response(JSON.stringify(first), { status: 200 }))
      .mockResolvedValueOnce(new Response("no", { status: 503 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(second), { status: 200 }));
    const { result } = renderHook(() => useUnifiedWorkSearch("复购"));

    await act(async () => vi.advanceTimersByTimeAsync(250));
    expect(result.current.status).toBe("ready");
    expect(result.current.moreSessions).toMatchObject({
      items: [],
      nextCursor: "c1",
      total: 5,
    });

    await act(async () => result.current.loadMoreSessions());
    expect(result.current.moreSessions).toMatchObject({
      items: [],
      nextCursor: "c1",
      error: true,
      loading: false,
    });

    await act(async () => result.current.loadMoreSessions());
    const [, retryOptions] = vi.mocked(fetch).mock.calls[2] ?? [];
    expect(JSON.parse(String(retryOptions?.body))).toEqual({
      query: "复购",
      limit: 20,
      kinds: ["session"],
      cursor: "c1",
    });
    expect(result.current.moreSessions.items.map((item) => item.id)).toEqual([
      "s2",
      "s3",
      "s4",
    ]);
    expect(result.current.moreSessions.nextCursor).toBe("c2");
    expect(result.current.moreSessions.error).toBe(false);
  });

  it("parses the content match, group total and cursor", async () => {
    vi.useRealTimers();
    vi.mocked(fetch).mockResolvedValue(
      new Response(
        JSON.stringify({
          query: "复购",
          partial: false,
          groups: [
            {
              kind: "session",
              unavailable: false,
              total: 12,
              next_cursor: "abc",
              items: [
                {
                  kind: "session",
                  id: "s1",
                  title: "2026年6月会员分析",
                  session_id: "s1",
                  snippet: "看一下老客复购周期",
                  updated_at: null,
                  match: "content",
                },
              ],
            },
            { kind: "report", unavailable: false, items: [] },
            { kind: "file", unavailable: false, items: [] },
            { kind: "knowledge", unavailable: false, items: [] },
          ],
        }),
        { status: 200 },
      ),
    );

    const response = await fetchUnifiedWorkSearch("复购");

    expect(response.groups[0]).toMatchObject({ total: 12, nextCursor: "abc" });
    expect(response.groups[0]?.items[0]?.match).toBe("content");
    expect(response.groups[1]).toMatchObject({ total: null, nextCursor: null });
  });

  it("builds stable deep links for every result kind", () => {
    const base = {
      id: "id 1",
      title: "Q2 report.csv",
      session_id: "s 1",
      snippet: null,
      updated_at: null,
      created_at: null,
      match: null,
    };
    expect(unifiedWorkResultHref({ ...base, kind: "session" })).toBe(
      "/?session=s%201",
    );
    expect(unifiedWorkResultHref({ ...base, kind: "report" })).toBe(
      "/?session=s%201&report=1",
    );
    expect(unifiedWorkResultHref({ ...base, kind: "file" })).toBe(
      "/files/?focus=id%201",
    );
    expect(unifiedWorkResultHref({ ...base, kind: "knowledge" })).toBe(
      "/knowledge/?file_id=id%201",
    );
  });

  it("counts results from the group totals, growing with Show more", () => {
    const group = (kind: "session" | "report" | "file" | "knowledge", items: number, total: number | null = null) => ({
      kind,
      unavailable: false,
      timedOut: false,
      total,
      nextCursor: kind === "session" && total !== null && total > items ? "next" : null,
      items: Array.from({ length: items }, (_, i) => ({
        kind,
        id: `${kind}_${i}`,
        title: `${kind} ${i}`,
        session_id: null,
        snippet: null,
        updated_at: null,
        created_at: null,
        match: null,
      })),
    });
    // The dogfood case: sessions say 78 while the announcement said 16 (the items on screen).
    const groups = [group("session", 5, 78), group("report", 4), group("file", 0), group("knowledge", 2)];
    expect(searchResultCount(groups)).toEqual({ count: 84, atLeast: false });
    // After "Show more" the session total comes from the paged response.
    expect(searchResultCount(groups, 80)).toEqual({ count: 86, atLeast: false });
    // A full first page without a total may be cut: the count is a floor.
    expect(searchResultCount([group("session", 3, 3), group("report", 5)])).toEqual({ count: 8, atLeast: true });
    // An unavailable source counts nothing and makes the sum a floor.
    expect(searchResultCount([group("session", 3, 3), { ...group("report", 2), unavailable: true }])).toEqual({ count: 3, atLeast: true });
  });
});
