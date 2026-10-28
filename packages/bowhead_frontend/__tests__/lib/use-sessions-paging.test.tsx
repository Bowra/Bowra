/**
 * Session list paging: older pages are appended and deduped, the 10s poll of
 * the first page keeps them, and a moved first-page boundary re-fetches the
 * loaded chain so no row falls between the first page and the older pages.
 */
import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { SessionGroupDTO, SessionListResponseDTO } from "@/lib/api";

vi.mock("@/lib/api", () => ({
  fetchSessions: vi.fn(),
  fetchWorkspaces: vi.fn(async () => ({
    items: [{ id: "ws_a", name: "Space A", desc: "" }],
    active_id: "ws_a",
  })),
  setLastWorkspace: vi.fn(async () => {}),
}));

vi.stubEnv("NEXT_PUBLIC_BOWHEAD_SSE_MODE", "sse");

const { fetchSessions } = await import("@/lib/api");
const {
  useSessionList,
  useSessionSearch,
  sessionsResource,
  sessionPages,
  createSessionPagesStore,
} = await import("@/lib/use-sessions");
const { mergeSessionGroups } = await import("@/lib/session-groups");
const { spacesResource } = await import("@/lib/use-space");
const { SESSION_LIST_CHANGED_EVENT } =
  await import("@/lib/session-list-events");

function group(
  id: string,
  ids: string[],
  pinned: string[] = [],
): SessionGroupDTO {
  return {
    id,
    label: id.toUpperCase(),
    items: ids.map((sid) => ({
      id: sid,
      title: sid,
      state: "done" as const,
      pinned: pinned.includes(sid) || undefined,
    })),
  };
}

function page(
  groups: SessionGroupDTO[],
  nextCursor: string | null,
): SessionListResponseDTO {
  return {
    groups,
    archived_count: 0,
    next_cursor: nextCursor,
    has_more: nextCursor !== null,
  };
}

/** Server double: first page + cursor pages, swappable mid-test. */
let first: SessionListResponseDTO;
let byCursor: Record<string, SessionListResponseDTO>;

beforeEach(() => {
  sessionsResource.reset();
  sessionPages.reset();
  spacesResource.reset();
  window.localStorage?.setItem?.("bowhead.space_id", "ws_a");
  first = page(
    [group("today", ["s1", "s2"]), group("older", ["s9"], ["s9"])],
    "c1",
  );
  byCursor = {
    c1: page([group("today", []), group("older", ["s3", "s4"])], "c2"),
    c2: page([group("older", ["s9", "s5"])], null),
  };
  vi.mocked(fetchSessions).mockReset();
  vi.mocked(fetchSessions).mockImplementation(async (options = {}) => {
    if (options.cursor) {
      const response = byCursor[options.cursor];
      if (!response) throw new Error(`unknown cursor ${options.cursor}`);
      return response;
    }
    return first;
  });
});

function ids(groups: { id: string; items: { id: string }[] }[]) {
  return Object.fromEntries(
    groups.map((g) => [g.id, g.items.map((item) => item.id)]),
  );
}

describe("mergeSessionGroups", () => {
  it("appends by group id and keeps the first copy of a session", () => {
    const merged = mergeSessionGroups(
      [
        {
          id: "today",
          label: "T",
          items: [
            { id: "a", title: "new", intents: [], time: "", state: "done" },
          ],
        },
      ],
      [
        {
          id: "today",
          label: "T",
          items: [
            { id: "a", title: "stale", intents: [], time: "", state: "done" },
          ],
        },
        {
          id: "older",
          label: "O",
          items: [
            { id: "b", title: "b", intents: [], time: "", state: "done" },
          ],
        },
      ],
    );
    expect(ids(merged)).toEqual({ today: ["a"], older: ["b"] });
    expect(merged[0].items[0].title).toBe("new");
  });
});

describe("useSessionList paging", () => {
  it("loads older pages, dedupes repeats and reports the end", async () => {
    const { result } = renderHook(() => useSessionList(0, true, "ws_a"));
    await waitFor(() => expect(result.current.groups.length).toBe(2));
    expect(result.current.hasMore).toBe(true);

    await act(() => result.current.loadMore());
    expect(ids(result.current.groups)).toEqual({
      today: ["s1", "s2"],
      older: ["s9", "s3", "s4"],
    });
    expect(result.current.hasMore).toBe(true);

    await act(() => result.current.loadMore());
    // The pinned s9 was already on the first page; the stream repeats it.
    expect(ids(result.current.groups)).toEqual({
      today: ["s1", "s2"],
      older: ["s9", "s3", "s4", "s5"],
    });
    expect(result.current.hasMore).toBe(false);
    expect(
      vi
        .mocked(fetchSessions)
        .mock.calls.map(([options]) => options?.cursor ?? null),
    ).toEqual([null, "c1", "c2"]);
  });

  it("keeps loaded pages when the poll refreshes an unchanged boundary", async () => {
    const { result } = renderHook(() => useSessionList(0, true, "ws_a"));
    await waitFor(() => expect(result.current.hasMore).toBe(true));
    await act(() => result.current.loadMore());

    // Same boundary, fresher first page (a title changed).
    first = page(
      [group("today", ["s1", "s2"]), group("older", ["s9"], ["s9"])],
      "c1",
    );
    first.groups[0].items[0].title = "renamed";
    vi.mocked(fetchSessions).mockClear();
    await act(async () => {
      await sessionsResource.refresh("ws_a");
    });

    expect(result.current.groups[0].items[0].title).toBe("renamed");
    expect(ids(result.current.groups).older).toEqual(["s9", "s3", "s4"]);
    // Only the first page was requested.
    expect(
      vi.mocked(fetchSessions).mock.calls.map(([options]) => options?.cursor),
    ).toEqual([undefined]);
  });

  it("re-walks the loaded chain when the first-page boundary moves", async () => {
    const { result } = renderHook(() => useSessionList(0, true, "ws_a"));
    await waitFor(() => expect(result.current.hasMore).toBe(true));
    await act(() => result.current.loadMore());

    // A new session pushed s2 off the first page; the server cursor now starts at s2.
    first = page(
      [group("today", ["s0", "s1"]), group("older", ["s9"], ["s9"])],
      "c1b",
    );
    byCursor.c1b = page(
      [group("today", ["s2"]), group("older", ["s3"])],
      "c2b",
    );
    vi.mocked(fetchSessions).mockClear();
    await act(async () => {
      await sessionsResource.refresh("ws_a");
    });

    await waitFor(() =>
      expect(ids(result.current.groups)).toEqual({
        today: ["s0", "s1", "s2"],
        older: ["s9", "s3"],
      }),
    );
    // One page was loaded before, so exactly one page is re-fetched.
    expect(
      vi.mocked(fetchSessions).mock.calls.map(([options]) => options?.cursor),
    ).toEqual([undefined, "c1b"]);
    expect(result.current.hasMore).toBe(true);
  });

  it("re-walks after a deliberate list change even with the same boundary", async () => {
    const { result } = renderHook(() => useSessionList(0, true, "ws_a"));
    await waitFor(() => expect(result.current.hasMore).toBe(true));
    await act(() => result.current.loadMore());

    // s4 (an older-page row) was archived; the boundary did not move.
    byCursor.c1 = page([group("older", ["s3"])], "c2");
    first = { ...first, archived_count: 1 };
    await act(async () => {
      window.dispatchEvent(new Event(SESSION_LIST_CHANGED_EVENT));
      await new Promise((resolve) => setTimeout(resolve, 80));
    });

    await waitFor(() =>
      expect(ids(result.current.groups).older).toEqual(["s9", "s3"]),
    );
    expect(result.current.archivedCount).toBe(1);
  });

  it("keeps loaded rows and the cursor when a page fails", async () => {
    const { result } = renderHook(() => useSessionList(0, true, "ws_a"));
    await waitFor(() => expect(result.current.hasMore).toBe(true));
    await act(() => result.current.loadMore());
    delete byCursor.c2;

    await act(() => result.current.loadMore());
    expect(result.current.loadMoreError).toBe(true);
    expect(ids(result.current.groups).older).toEqual(["s9", "s3", "s4"]);
    expect(result.current.hasMore).toBe(true);
  });

  it("never leaves loadingMore stuck when a rewalk supersedes a load-more", async () => {
    let releaseOld: (value: {
      groups: never[];
      nextCursor: null;
    }) => void = () => {};
    const fetchPage = vi.fn(async (_scope: string, cursor: string) => {
      if (cursor === "c1") return { groups: [], nextCursor: "c2" };
      if (cursor === "c2")
        return new Promise<{ groups: never[]; nextCursor: null }>((resolve) => {
          releaseOld = resolve;
        });
      return { groups: [], nextCursor: "c9" };
    });
    const store = createSessionPagesStore(fetchPage);
    await store.loadMore("k", "c1", "ws_a");
    const pending = store.loadMore("k", "c1", "ws_a");
    expect(store.peek("k").loadingMore).toBe(true);

    // The first-page boundary moves while c2 is in flight.
    store.reconcile("k", "c1b", "ws_a");
    await waitFor(() => expect(store.peek("k").anchor).toBe("c1b"));
    expect(store.peek("k").loadingMore).toBe(false);

    // The superseded response lands late and must change nothing.
    releaseOld({ groups: [], nextCursor: null });
    await pending;
    expect(store.peek("k").loadingMore).toBe(false);
    expect(store.peek("k").pages).toHaveLength(1);
  });

  it("releases loadingMore when the superseding rewalk fails", async () => {
    const fetchPage = vi.fn(async (_scope: string, cursor: string) => {
      if (cursor === "c1") return { groups: [], nextCursor: "c2" };
      if (cursor === "c2") return new Promise<never>(() => {});
      throw new Error("offline");
    });
    const store = createSessionPagesStore(fetchPage);
    await store.loadMore("k", "c1", "ws_a");
    void store.loadMore("k", "c1", "ws_a");
    expect(store.peek("k").loadingMore).toBe(true);

    store.reconcile("k", "c1b", "ws_a");
    await waitFor(() => expect(store.peek("k").loadingMore).toBe(false));
    // The previous chain stays on screen.
    expect(store.peek("k").pages).toHaveLength(1);
  });

  it("requests older pages for the Space the first page was fetched for", async () => {
    const fetchPage = vi.fn(async () => ({ groups: [], nextCursor: "c2" }));
    const store = createSessionPagesStore(fetchPage);
    // Key "" follows the active Space; its first page was fetched for ws_a.
    await store.loadMore("", "c1", "ws_a");
    // The user switched Space; the chain keeps its own Space.
    await store.loadMore("", "c1", "ws_b");
    expect(fetchPage.mock.calls).toEqual([
      ["ws_a", "c1"],
      ["ws_a", "c2"],
    ]);
    // A first page fetched for another Space restarts the chain there.
    store.reconcile("", "d1", "ws_b");
    await waitFor(() => expect(store.peek("").scope).toBe("ws_b"));
    // Same number of pages (two), all for ws_b.
    await waitFor(() => expect(fetchPage).toHaveBeenCalledTimes(4));
    expect(fetchPage.mock.calls.slice(2)).toEqual([
      ["ws_b", "d1"],
      ["ws_b", "c2"],
    ]);
  });

  it("treats a gateway without cursors as a single page", async () => {
    first = { groups: [group("today", ["s1"])], archived_count: 0 };
    const { result } = renderHook(() => useSessionList(0, true, "ws_a"));
    await waitFor(() => expect(result.current.groups.length).toBe(1));
    expect(result.current.hasMore).toBe(false);
    await act(() => result.current.loadMore());
    expect(fetchSessions).toHaveBeenCalledTimes(1);
  });
});

describe("useSessionSearch", () => {
  it("pages a query and restarts when the query changes", async () => {
    vi.mocked(fetchSessions).mockImplementation(async (options = {}) => {
      if (options.query === "alpha" && !options.cursor)
        return page([group("older", ["a1"])], "q1");
      if (options.cursor === "q1") return page([group("older", ["a2"])], null);
      if (options.query === "beta") return page([group("today", ["b1"])], null);
      throw new Error("unexpected");
    });
    const { result, rerender } = renderHook(
      ({ query }) => useSessionSearch({ query }),
      {
        initialProps: { query: "alpha" },
      },
    );
    await waitFor(() =>
      expect(ids(result.current.groups)).toEqual({ older: ["a1"] }),
    );
    expect(result.current.hasMore).toBe(true);
    await act(() => result.current.loadMore());
    expect(ids(result.current.groups)).toEqual({ older: ["a1", "a2"] });
    expect(result.current.hasMore).toBe(false);

    rerender({ query: "beta" });
    await waitFor(() =>
      expect(ids(result.current.groups)).toEqual({ today: ["b1"] }),
    );
    expect(vi.mocked(fetchSessions).mock.calls.at(-1)?.[0]).toMatchObject({
      query: "beta",
      cursor: null,
    });
  });
});
