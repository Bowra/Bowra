import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";

const fetchSessionsMock = vi.fn();
const spaceState = {
  items: [] as Array<{ id: string; name: string }>,
  activeId: null as string | null,
  loading: true,
};

vi.mock("./api", () => ({
  fetchSessions: (...args: unknown[]) => fetchSessionsMock(...args),
}));
vi.mock("./use-space", () => ({
  useSpaceSelection: () => spaceState,
}));

import { sessionsResource, useSessionList } from "./use-sessions";

const SESSION_ROW = {
  id: "s_1",
  title: "Q3 decline",
  updated_at: "2026-10-07T10:00:00Z",
};

describe("useSessionList before the space resolves", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_BOWHEAD_SSE_MODE", "sse");
    sessionsResource.reset();
    fetchSessionsMock.mockReset();
    spaceState.items = [];
    spaceState.activeId = "ws_remembered";
    spaceState.loading = true;
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    sessionsResource.reset();
  });

  it("keeps loading when the unvalidated remembered space answers with an empty list", async () => {
    fetchSessionsMock.mockResolvedValue({ groups: [], archived_count: 0 });
    const { result } = renderHook(() => useSessionList());
    await waitFor(() => expect(fetchSessionsMock).toHaveBeenCalled());
    await waitFor(() => expect(sessionsResource.peek("ws_remembered").status).toBe("success"));
    // An empty answer for a provisional scope is not "No sessions yet".
    expect(result.current.loading).toBe(true);
    expect(result.current.error).toBe(false);
  });

  it("also waits when the page passes the same provisional id explicitly", async () => {
    fetchSessionsMock.mockResolvedValue({ groups: [], archived_count: 0 });
    const { result } = renderHook(() => useSessionList(0, true, "ws_remembered"));
    await waitFor(() => expect(sessionsResource.peek("ws_remembered").status).toBe("success"));
    expect(result.current.loading).toBe(true);
  });

  it("hides a failed provisional load instead of showing the error row", async () => {
    fetchSessionsMock.mockRejectedValue(new Error("forbidden"));
    const { result } = renderHook(() => useSessionList());
    await waitFor(() => expect(sessionsResource.peek("ws_remembered").status).toBe("error"));
    expect(result.current.error).toBe(false);
    expect(result.current.loading).toBe(true);
  });

  it("reports an empty list only after the space resolves and its load completes", async () => {
    fetchSessionsMock.mockResolvedValue({ groups: [], archived_count: 0 });
    const { result, rerender } = renderHook(() => useSessionList());
    await waitFor(() => expect(sessionsResource.peek("ws_remembered").status).toBe("success"));
    expect(result.current.loading).toBe(true);

    spaceState.items = [{ id: "ws_real", name: "East" }];
    spaceState.activeId = "ws_real";
    spaceState.loading = false;
    rerender();
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(fetchSessionsMock).toHaveBeenLastCalledWith({ workspaceId: "ws_real" });
    expect(result.current.groups).toEqual([]);
  });

  it("shows rows from the early fetch while the space is still resolving", async () => {
    fetchSessionsMock.mockResolvedValue({
      groups: [{ id: "today", label: "today", items: [SESSION_ROW] }],
      archived_count: 0,
    });
    const { result } = renderHook(() => useSessionList());
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.groups[0]?.items).toHaveLength(1);
  });
});

describe("useSessionList on a slow cold load (116 dogfood 1.5.25: drawer stuck on the skeleton)", () => {
  type Deferred = { resolve: (value: unknown) => void; reject: (error: unknown) => void };
  let calls: Deferred[];

  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_BOWHEAD_SSE_MODE", "sse");
    sessionsResource.reset();
    fetchSessionsMock.mockReset();
    calls = [];
    // Every request stays pending until the test settles it; a refresh on a slow gateway may take as long as it likes.
    fetchSessionsMock.mockImplementation(
      () => new Promise((resolve, reject) => calls.push({ resolve, reject })),
    );
    // Auth and the space list are still resolving: the remembered space is provisional.
    spaceState.items = [];
    spaceState.activeId = "ws_remembered";
    spaceState.loading = true;
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    sessionsResource.reset();
  });

  const ROWS = { groups: [{ id: "today", label: "today", items: [SESSION_ROW] }], archived_count: 0 };
  const NEWER = {
    groups: [{ id: "today", label: "today", items: [{ ...SESSION_ROW, id: "s_2" }, SESSION_ROW] }],
    archived_count: 0,
  };
  const ids = (groups: Array<{ items: Array<{ id: string }> }>) => groups.flatMap((group) => group.items.map((item) => item.id));

  it("shows the first answer even when a broadcast superseded it while it was in flight", async () => {
    const { result, rerender } = renderHook(() => useSessionList());
    await waitFor(() => expect(calls).toHaveLength(1));
    // A turn finishing during hydration broadcasts "the server state moved": the resource refreshes.
    window.dispatchEvent(new Event("bowhead.canvas.changed"));
    await waitFor(() => expect(calls).toHaveLength(2));
    // The first request answers 200 with rows; the refresh is still on its (slow) way.
    await act(async () => {
      calls[0]!.resolve(ROWS);
    });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(ids(result.current.groups)).toEqual(["s_1"]);

    // The space resolves to the same id; the list stays.
    spaceState.items = [{ id: "ws_remembered", name: "East" }];
    spaceState.loading = false;
    rerender();
    expect(result.current.loading).toBe(false);

    // The refresh lands later and replaces the first answer.
    await act(async () => {
      calls[1]!.resolve(NEWER);
    });
    await waitFor(() => expect(ids(result.current.groups)).toEqual(["s_2", "s_1"]));
  });

  it("an answer that arrives after the refresh landed does not overwrite it", async () => {
    const { result } = renderHook(() => useSessionList());
    await waitFor(() => expect(calls).toHaveLength(1));
    window.dispatchEvent(new Event("bowhead:sessions-changed"));
    await waitFor(() => expect(calls).toHaveLength(2));
    await act(async () => {
      calls[1]!.resolve(NEWER);
    });
    await waitFor(() => expect(result.current.loading).toBe(false));
    await act(async () => {
      calls[0]!.resolve(ROWS);
    });
    expect(ids(result.current.groups)).toEqual(["s_2", "s_1"]);
  });

  it("a team switch still discards the old team's answer in flight", async () => {
    const { result } = renderHook(() => useSessionList());
    await waitFor(() => expect(calls).toHaveLength(1));
    window.dispatchEvent(new CustomEvent("bowhead:team-changed", { detail: { teamId: "team_2" } }));
    await waitFor(() => expect(calls).toHaveLength(2));
    await act(async () => {
      calls[0]!.resolve(ROWS);
    });
    expect(result.current.loading).toBe(true);
    expect(result.current.groups).toEqual([]);
  });
});
