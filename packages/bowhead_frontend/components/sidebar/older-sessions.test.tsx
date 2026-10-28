import {
  act,
  cleanup,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { NextIntlClientProvider } from "next-intl";

import { renderIntl } from "@/__tests__/intl-render";
import zhMessages from "@/messages/zh-CN.json";
import { SKELETON_DELAY_MS } from "@/components/ui/loading-skeleton";
import type { SessionGroup } from "@/lib/types";

import {
  OlderSessionsRow,
  type OlderSessionsPaging,
} from "./older-sessions-row";
import { WorkRecovery } from "./work-recovery";

function withIntl(ui: React.ReactElement) {
  return (
    <NextIntlClientProvider locale="zh-CN" messages={zhMessages}>
      {ui}
    </NextIntlClientProvider>
  );
}

function paging(
  overrides: Partial<OlderSessionsPaging> = {},
): OlderSessionsPaging {
  return {
    hasMore: true,
    loadingMore: false,
    loadMoreError: false,
    olderPagesLoaded: 0,
    onLoadMore: vi.fn(),
    ...overrides,
  };
}

function session(
  id: string,
  title: string,
  extra: Partial<SessionGroup["items"][number]> = {},
) {
  return {
    id,
    title,
    intents: [],
    time: "3d",
    state: "done" as const,
    ...extra,
  };
}

const GROUPS: SessionGroup[] = [
  {
    id: "last7",
    label: "LAST 7 DAYS",
    items: [session("s_recent", "华东区9月GMV环比变化")],
  },
  {
    id: "older",
    label: "OLDER",
    items: [
      session("s_q2", "Q2 复盘报告初稿"),
      session("s_store", "门店客流归因"),
    ],
  },
];

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("OlderSessionsRow", () => {
  it("offers the next batch as a quiet row and loads on click or Enter", () => {
    const onLoadMore = vi.fn();
    renderIntl(<OlderSessionsRow paging={paging({ onLoadMore })} />, {
      locale: "zh-CN",
    });

    const row = screen.getByRole("button", { name: "更早的会话" });
    fireEvent.click(row);
    expect(onLoadMore).toHaveBeenCalledTimes(1);
    // A native button: Enter activates it without a custom key handler.
    expect(row.tagName).toBe("BUTTON");
  });

  it("shows three skeleton rows while loading, after the shared delay", () => {
    vi.useFakeTimers();
    const { container } = renderIntl(
      <OlderSessionsRow paging={paging({ loadingMore: true })} />,
      {
        locale: "zh-CN",
      },
    );

    const skeleton = screen.getByRole("status", { name: "正在加载会话" });
    expect(skeleton.className).toContain("is-pending");
    expect(container.querySelectorAll(".ui-skeleton-row")).toHaveLength(3);
    act(() => {
      vi.advanceTimersByTime(SKELETON_DELAY_MS);
    });
    expect(skeleton.className).not.toContain("is-pending");
    expect(screen.queryByRole("button", { name: "更早的会话" })).toBeNull();
  });

  it("replaces the row with an error and retries the same batch", () => {
    const onLoadMore = vi.fn();
    renderIntl(
      <OlderSessionsRow paging={paging({ loadMoreError: true, onLoadMore })} />,
      {
        locale: "zh-CN",
      },
    );

    expect(screen.getByRole("status").textContent).toContain(
      "没有加载出更早的会话",
    );
    expect(screen.queryByRole("button", { name: "更早的会话" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(onLoadMore).toHaveBeenCalledTimes(1);
  });

  it("ends with a closing line only after at least one older batch", () => {
    const { rerender } = renderIntl(
      <OlderSessionsRow
        paging={paging({ hasMore: false, olderPagesLoaded: 0 })}
      />,
      { locale: "zh-CN" },
    );
    // The list fit one batch: no row at all.
    expect(screen.queryByText("没有更早的会话了")).toBeNull();
    expect(screen.queryByRole("button")).toBeNull();

    rerender(
      withIntl(
        <OlderSessionsRow
          paging={paging({ hasMore: false, olderPagesLoaded: 2 })}
        />,
      ),
    );
    expect(screen.getByText("没有更早的会话了")).toBeTruthy();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("uses the English copy from the spec", () => {
    renderIntl(<OlderSessionsRow paging={paging()} />, { locale: "en-US" });
    expect(screen.getByRole("button", { name: "Show older" })).toBeTruthy();
  });

  it("is 44 high in the phone drawer", () => {
    renderIntl(<OlderSessionsRow paging={paging()} surface="drawer" />, {
      locale: "zh-CN",
    });
    expect(
      screen.getByRole("button", { name: "更早的会话" }).className,
    ).toContain("is-touch");
  });
});

function renderRecovery(
  overrides: Partial<React.ComponentProps<typeof WorkRecovery>> = {},
  locale: "zh-CN" | "en-US" = "zh-CN",
) {
  const props: React.ComponentProps<typeof WorkRecovery> = {
    groups: GROUPS,
    currentSessionId: null,
    query: "",
    onQueryChange: vi.fn(),
    onPick: vi.fn(),
    ...overrides,
  };
  return { ...renderIntl(<WorkRecovery {...props} />, { locale }), props };
}

describe("WorkRecovery older sessions", () => {
  it("puts the row after the last session of the Older group and calls loadMore", () => {
    const olderSessions = paging();
    const { container } = renderRecovery({ olderSessions });

    const row = screen.getByRole("button", { name: "更早的会话" });
    const rows = Array.from(
      container.querySelectorAll("[data-session-id], .older-sessions"),
    );
    expect(rows.at(-1)).toBe(row.closest(".older-sessions"));
    expect(rows.at(-2)?.getAttribute("data-session-id")).toBe("s_store");
    fireEvent.click(row);
    expect(olderSessions.onLoadMore).toHaveBeenCalledTimes(1);
  });

  it("moves focus to the first session of the new page, not a row a poll added on top", () => {
    const olderSessions = paging();
    const { props, rerender } = renderRecovery({ olderSessions });
    fireEvent.click(screen.getByRole("button", { name: "更早的会话" }));

    rerender(
      withIntl(
        <WorkRecovery
          {...props}
          olderSessions={paging({ loadingMore: true })}
        />,
      ),
    );
    const nextGroups: SessionGroup[] = [
      // The 10s poll brought a brand-new session in at the top meanwhile.
      {
        id: "today",
        label: "TODAY",
        items: [session("s_new", "刚开始的会话")],
      },
      GROUPS[0]!,
      {
        ...GROUPS[1]!,
        items: [
          ...GROUPS[1]!.items,
          session("s_old1", "2025 双十一复盘"),
          session("s_old2", "年度预算"),
        ],
      },
    ];
    rerender(
      withIntl(
        <WorkRecovery
          {...props}
          groups={nextGroups}
          olderSessions={paging({
            olderPagesLoaded: 1,
            latestOlderPageIds: ["s_old1", "s_old2"],
          })}
        />,
      ),
    );

    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: /2025 双十一复盘/ }),
    );
    expect(document.querySelector(".unified-work-announcer")?.textContent).toBe(
      "已加载 2 个更早的会话",
    );
  });

  it("scrolls a virtualized list to the first new session and focuses it (116 dogfood 1.5.25: focus fell to body)", () => {
    // A 400px viewport, so the virtualizer renders a window instead of nothing (jsdom has no layout).
    const sized = ["offsetHeight", "offsetWidth", "clientHeight", "clientWidth"] as const;
    const originals = sized.map((key) => [key, Object.getOwnPropertyDescriptor(HTMLElement.prototype, key)] as const);
    for (const key of sized) {
      Object.defineProperty(HTMLElement.prototype, key, { configurable: true, get: () => 400 });
    }
    try {
    const many: SessionGroup[] = [
      {
        id: "older",
        label: "OLDER",
        items: Array.from({ length: 60 }, (_, i) => session(`s_${i}`, `会话 ${i}`)),
      },
    ];
    const { props, rerender } = renderRecovery({ groups: many, olderSessions: paging() });
    expect(document.querySelector(".unified-work-virtual-list")).not.toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "更早的会话" }));
    rerender(withIntl(<WorkRecovery {...props} olderSessions={paging({ loadingMore: true })} />));
    const nextGroups: SessionGroup[] = [
      { ...many[0]!, items: [...many[0]!.items, session("s_old1", "2025 双十一复盘"), session("s_old2", "年度预算")] },
    ];
    // Positive control for the cause: the new row is outside the rendered window.
    rerender(withIntl(<WorkRecovery {...props} groups={nextGroups} olderSessions={paging({ loadingMore: true })} />));
    expect(document.querySelector('[data-session-id="s_old1"]')).toBeNull();

    rerender(
      withIntl(
        <WorkRecovery
          {...props}
          groups={nextGroups}
          olderSessions={paging({ olderPagesLoaded: 1, latestOlderPageIds: ["s_old1", "s_old2"] })}
        />,
      ),
    );

    expect(document.activeElement).not.toBe(document.body);
    expect(document.activeElement?.closest("[data-session-id]")?.getAttribute("data-session-id")).toBe("s_old1");
    } finally {
      for (const [key, descriptor] of originals) {
        if (descriptor) Object.defineProperty(HTMLElement.prototype, key, descriptor);
        else delete (HTMLElement.prototype as unknown as Record<string, unknown>)[key];
      }
    }
  });

  it("moves focus to Retry when the batch fails", () => {
    const { props, rerender } = renderRecovery({ olderSessions: paging() });
    fireEvent.click(screen.getByRole("button", { name: "更早的会话" }));
    rerender(
      withIntl(
        <WorkRecovery
          {...props}
          olderSessions={paging({ loadingMore: true })}
        />,
      ),
    );
    rerender(
      withIntl(
        <WorkRecovery
          {...props}
          olderSessions={paging({ loadMoreError: true })}
        />,
      ),
    );

    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "重试" }),
    );
  });

  it("shows the end line once everything older is loaded", () => {
    renderRecovery({
      olderSessions: paging({ hasMore: false, olderPagesLoaded: 3 }),
    });
    expect(screen.getByText("没有更早的会话了")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "更早的会话" })).toBeNull();
  });

  it("keeps loaded rows and offers Retry when a batch fails", () => {
    const onLoadMore = vi.fn();
    renderRecovery({
      olderSessions: paging({
        loadMoreError: true,
        onLoadMore,
        olderPagesLoaded: 1,
      }),
    });

    expect(screen.getByRole("button", { name: /门店客流归因/ })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(onLoadMore).toHaveBeenCalledTimes(1);
  });

  it("shows a pinned session once even when an older page repeats it", () => {
    // mergeSessionGroups drops the repeat; the pinned section takes the row out of its date group.
    const groups: SessionGroup[] = [
      {
        id: "last7",
        label: "LAST 7 DAYS",
        items: [session("s_recent", "最近")],
      },
      {
        id: "older",
        label: "OLDER",
        items: [
          session("s_pin", "置顶的旧会话", { pinned: true }),
          session("s_old", "旧会话"),
        ],
      },
    ];
    renderRecovery({ groups, olderSessions: paging() });

    expect(
      screen.getAllByRole("button", { name: /置顶的旧会话/ }),
    ).toHaveLength(1);
    expect(screen.getByRole("heading", { name: "置顶" })).toBeTruthy();
  });

  it("does not render without paging, while the first load is pending, or while searching", () => {
    const { unmount } = renderRecovery();
    expect(screen.queryByRole("button", { name: "更早的会话" })).toBeNull();
    unmount();

    const pending = renderRecovery({
      olderSessions: paging(),
      listStatusSlot: <div>loading</div>,
    });
    expect(screen.queryByRole("button", { name: "更早的会话" })).toBeNull();
    pending.unmount();

    vi.stubGlobal(
      "fetch",
      vi.fn(() => new Promise<Response>(() => {})),
    );
    renderRecovery({ olderSessions: paging(), query: "复购" });
    expect(screen.queryByRole("button", { name: "更早的会话" })).toBeNull();
  });
});

/** Answers /api/v1/search from `responses` in order; anything else (metrics) gets an empty 204. */
function searchFetch(...responses: Response[]) {
  const searchCalls: RequestInit[] = [];
  const fetchMock = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      if (!String(input).includes("/api/v1/search"))
        return new Response(null, { status: 204 });
      searchCalls.push(init ?? {});
      return responses.shift() ?? new Response("no", { status: 503 });
    },
  );
  vi.stubGlobal("fetch", fetchMock);
  return searchCalls;
}

function searchResponse(groups: unknown[]) {
  return new Response(
    JSON.stringify({ query: "复购", partial: false, groups }),
    { status: 200 },
  );
}

function sessionHit(
  id: string,
  title: string,
  extra: Record<string, unknown> = {},
) {
  return {
    kind: "session",
    id,
    title,
    session_id: id,
    snippet: null,
    updated_at: null,
    match: "title",
    ...extra,
  };
}

describe("WorkRecovery sidebar search", () => {
  beforeEach(() => {
    window.localStorage?.clear?.();
  });

  it("counts every match, tags content hits with a snippet and loads more sessions in place", async () => {
    const searchCalls = searchFetch(
      searchResponse([
        {
          kind: "session",
          unavailable: false,
          total: 12,
          next_cursor: "c1",
          items: [
            sessionHit("s1", "会员复购率下滑原因"),
            sessionHit("s2", "2026年6月会员分析", {
              match: "content",
              snippet:
                "我们这个季度想先看一下老客复购周期，再看新客贡献和渠道差异",
            }),
            sessionHit("s3", "Q2 复盘报告初稿"),
          ],
        },
        {
          kind: "report",
          unavailable: false,
          items: [
            {
              kind: "report",
              id: "r1",
              title: "会员复购月报",
              session_id: "s1",
              snippet: null,
              updated_at: null,
            },
          ],
        },
        { kind: "file", unavailable: true, items: [] },
        { kind: "knowledge", unavailable: false, items: [] },
      ]),
      searchResponse([
        {
          kind: "session",
          unavailable: false,
          total: 12,
          next_cursor: null,
          items: [
            sessionHit("s3", "Q2 复盘报告初稿"),
            sessionHit("s4", "复购人群画像"),
          ],
        },
      ]),
    );
    renderRecovery({ query: "复购", olderSessions: paging() });

    const sessionsHeading = await screen.findByRole("heading", {
      name: /会话/,
    });
    expect(sessionsHeading.textContent).toContain("12 个");
    // The announced count agrees with the group totals (12 sessions + 1 report), not the 4 rows on screen; the file
    // source was unavailable, so it is a floor.
    expect(screen.getByText("找到 13+ 条结果")).toBeTruthy();
    // Reports cannot be counted server-side: no number rather than a wrong one.
    expect(screen.getByRole("heading", { name: "报告" })).toBeTruthy();

    const contentHit = screen.getByRole("link", { name: /2026年6月会员分析/ });
    expect(within(contentHit).getByText("内容")).toBeTruthy();
    // About 12 characters each side of the term, the term marked.
    expect(contentHit.querySelector(".is-content")?.textContent).toBe(
      "内容…看一下老客复购周期，再看新客贡献和渠道…",
    );
    expect(contentHit.querySelector("mark")?.textContent).toBe("复购");
    // Title hits mark the term in the title and have no content tag.
    const titleHit = screen.getByRole("link", { name: /会员复购率下滑原因/ });
    expect(within(titleHit).queryByText("内容")).toBeNull();
    expect(titleHit.querySelector("mark")?.textContent).toBe("复购");

    // A failed source says so in its own place instead of a banner over everything.
    expect(screen.getByText("文件搜索暂时不可用，其他结果正常")).toBeTruthy();

    fireEvent.click(
      screen.getByRole("button", { name: "显示更多会话（还有 9 个）" }),
    );
    await screen.findByRole("link", { name: /复购人群画像/ });
    expect(JSON.parse(String(searchCalls[1]?.body))).toEqual({
      query: "复购",
      limit: 20,
      kinds: ["session"],
      cursor: "c1",
    });
    // s3 repeated on the next page: shown once; the row disappears at the end.
    expect(
      screen.getAllByRole("link", { name: /Q2 复盘报告初稿/ }),
    ).toHaveLength(1);
    expect(screen.queryByRole("button", { name: /显示更多会话/ })).toBeNull();
    // Search never shows the older-sessions row.
    expect(screen.queryByRole("button", { name: "更早的会话" })).toBeNull();
  });

  it("explains the search scope when nothing matches", async () => {
    searchFetch(
      searchResponse([
        { kind: "session", unavailable: false, total: 0, items: [] },
        { kind: "report", unavailable: false, items: [] },
        { kind: "file", unavailable: false, items: [] },
        { kind: "knowledge", unavailable: false, items: [] },
      ]),
    );
    renderRecovery({ query: "复购" }, "en-US");

    expect(await screen.findByText("Nothing matches “复购”")).toBeTruthy();
    expect(
      screen.getByText(
        "Sessions are searched by title and your messages, across every Space you can access.",
      ),
    ).toBeTruthy();
  });

  it("keeps the button focusable while loading and moves focus to the new and then the last result", async () => {
    let releaseSecond: (response: Response) => void = () => {};
    const searchCalls = searchFetch(
      searchResponse([
        {
          kind: "session",
          unavailable: false,
          total: 3,
          next_cursor: "c1",
          items: [sessionHit("s1", "复购一")],
        },
        { kind: "report", unavailable: false, items: [] },
        { kind: "file", unavailable: false, items: [] },
        { kind: "knowledge", unavailable: false, items: [] },
      ]),
    );
    renderRecovery({ query: "复购" });
    const button = await screen.findByRole("button", {
      name: "显示更多会话（还有 2 个）",
    });
    vi.mocked(fetch).mockImplementationOnce(
      () => new Promise<Response>((resolve) => (releaseSecond = resolve)),
    );
    button.focus();
    fireEvent.click(button);
    await waitFor(() =>
      expect(button.getAttribute("aria-disabled")).toBe("true"),
    );
    expect(button.hasAttribute("disabled")).toBe(false);
    expect(document.activeElement).toBe(button);
    fireEvent.click(button);
    expect(
      vi
        .mocked(fetch)
        .mock.calls.filter(([url]) => String(url).includes("/api/v1/search")),
    ).toHaveLength(2);

    releaseSecond(
      searchResponse([
        {
          kind: "session",
          unavailable: false,
          total: null,
          next_cursor: "c2",
          items: [sessionHit("s2", "复购二")],
        },
      ]),
    );
    await waitFor(() =>
      expect(document.activeElement).toBe(
        screen.getByRole("link", { name: /复购二/ }),
      ),
    );
    expect(document.querySelector(".unified-work-announcer")?.textContent).toBe(
      "已加载 1 个，显示 2 / 共 3 个",
    );
    expect(searchCalls).toHaveLength(1);

    // The last page repeats a row: the button disappears and focus lands on the last result.
    vi.mocked(fetch).mockImplementationOnce(async () =>
      searchResponse([
        {
          kind: "session",
          unavailable: false,
          total: null,
          next_cursor: null,
          items: [sessionHit("s2", "复购二")],
        },
      ]),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "显示更多会话（还有 1 个）" }),
    );
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: /显示更多会话/ })).toBeNull(),
    );
    expect(document.activeElement).toBe(
      screen.getByRole("link", { name: /复购二/ }),
    );
  });

  it("moves focus to Retry when the next page fails", async () => {
    searchFetch(
      searchResponse([
        {
          kind: "session",
          unavailable: false,
          total: 6,
          next_cursor: "c1",
          items: [sessionHit("s1", "复购一")],
        },
        { kind: "report", unavailable: false, items: [] },
        { kind: "file", unavailable: false, items: [] },
        { kind: "knowledge", unavailable: false, items: [] },
      ]),
      new Response("no", { status: 503 }),
    );
    renderRecovery({ query: "复购" });
    fireEvent.click(
      await screen.findByRole("button", { name: "显示更多会话（还有 5 个）" }),
    );
    await waitFor(() =>
      expect(document.activeElement).toBe(
        screen.getByRole("button", { name: "重试" }),
      ),
    );
  });

  it("keeps loaded results and retries when the next page fails", async () => {
    const searchCalls = searchFetch(
      searchResponse([
        {
          kind: "session",
          unavailable: false,
          total: 6,
          next_cursor: "c1",
          items: [sessionHit("s1", "复购一")],
        },
        { kind: "report", unavailable: false, items: [] },
        { kind: "file", unavailable: false, items: [] },
        { kind: "knowledge", unavailable: false, items: [] },
      ]),
      new Response("no", { status: 503 }),
      searchResponse([
        {
          kind: "session",
          unavailable: false,
          total: 6,
          next_cursor: null,
          items: [sessionHit("s2", "复购二")],
        },
      ]),
    );
    renderRecovery({ query: "复购" });

    fireEvent.click(
      await screen.findByRole("button", { name: "显示更多会话（还有 5 个）" }),
    );
    await screen.findByText("没有加载出更多会话");
    expect(screen.getByRole("link", { name: /复购一/ })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() =>
      expect(screen.getByRole("link", { name: /复购二/ })).toBeTruthy(),
    );
    expect(searchCalls).toHaveLength(3);
  });
});
