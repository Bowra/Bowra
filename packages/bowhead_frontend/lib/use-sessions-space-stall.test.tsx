/**
 * 116 phone cold load, second cause of the drawer stuck on the skeleton: `/auth/me/workspaces` never answered.
 * The session list waits for the Space list (no remembered Space: no request at all; a remembered Space: its answer
 * is provisional), and nothing ended that wait. The real Space hook runs here with a never-resolving request.
 */
import { act, fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import enMessages from "@/messages/en-US.json";
import { sessionListStatus } from "@/components/sidebar/session-list-status";

const fetchWorkspacesMock = vi.fn();
const fetchSessionsMock = vi.fn();

vi.mock("./api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./api")>()),
  fetchWorkspaces: (...args: unknown[]) => fetchWorkspacesMock(...args),
  fetchSessions: (...args: unknown[]) => fetchSessionsMock(...args),
}));

import { SESSION_LIST_WAIT_MS, sessionsResource, useSessionList } from "./use-sessions";
import { spacesResource } from "./use-space";

const never = () => new Promise<never>(() => {});

function memoryStorage(): Storage {
  const values = new Map<string, string>();
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => void values.set(key, String(value)),
    removeItem: (key) => void values.delete(key),
    clear: () => values.clear(),
    key: (index) => [...values.keys()][index] ?? null,
    get length() {
      return values.size;
    },
  };
}

function ListStatus({ enabled = true }: { enabled?: boolean }) {
  const list = useSessionList(0, enabled);
  const hasRows = list.groups.some((group) => group.items.length > 0);
  return (
    <div>
      {sessionListStatus({
        loading: list.loading,
        error: list.error,
        stalledOn: list.stalledOn,
        hasRows,
        onRetry: list.retry,
      })}
      {hasRows ? <p>rows</p> : null}
    </div>
  );
}

function tree(enabled = true) {
  return (
    <NextIntlClientProvider locale="en-US" messages={enMessages}>
      <ListStatus enabled={enabled} />
    </NextIntlClientProvider>
  );
}

function renderList() {
  return render(tree());
}

async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

describe("session list while the Space list never answers", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.stubEnv("NEXT_PUBLIC_BOWHEAD_SSE_MODE", "sse");
    sessionsResource.reset();
    spacesResource.reset();
    fetchWorkspacesMock.mockReset().mockImplementation(never);
    fetchSessionsMock.mockReset().mockResolvedValue({ groups: [], archived_count: 0 });
    Object.defineProperty(window, "localStorage", { value: memoryStorage(), configurable: true });
    Object.defineProperty(window, "sessionStorage", { value: memoryStorage(), configurable: true });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllEnvs();
    sessionsResource.reset();
    spacesResource.reset();
  });

  it("first-time browser: skeleton for the bounded wait, then an actionable error naming the Space list", async () => {
    renderList();
    await advance(1_000);
    expect(screen.getByRole("status", { name: enMessages.session.rail_loading })).toBeTruthy();
    expect(fetchSessionsMock).not.toHaveBeenCalled();

    await advance(SESSION_LIST_WAIT_MS);
    expect(screen.getByText(enMessages.session.rail_load_stalled_spaces)).toBeTruthy();
    expect(screen.queryByRole("status", { name: enMessages.session.rail_loading })).toBeNull();
  });

  it("remembered Space with an empty provisional answer: same bounded wait", async () => {
    window.localStorage.setItem("bowhead.space_id", "ws_remembered");
    window.localStorage.setItem("bowhead.workspace_id", "ws_remembered");
    renderList();
    await advance(1_000);
    expect(fetchSessionsMock).toHaveBeenCalled();
    expect(screen.queryByText(enMessages.session.rail_load_stalled_spaces)).toBeNull();
    await advance(SESSION_LIST_WAIT_MS);
    expect(screen.getByText(enMessages.session.rail_load_stalled_spaces)).toBeTruthy();
  });

  it("Retry re-requests the Space list and restarts the wait; a late answer fills the list", async () => {
    renderList();
    await advance(SESSION_LIST_WAIT_MS + 1_000);
    expect(fetchWorkspacesMock).toHaveBeenCalledTimes(1);

    fetchWorkspacesMock.mockResolvedValue({ items: [{ id: "ws_1", name: "East", desc: "", role: "owner" }], active_id: "ws_1" });
    fetchSessionsMock.mockResolvedValue({
      groups: [{ id: "today", label: "TODAY", items: [{ id: "s_1", title: "Q3", state: "done" }] }],
      archived_count: 0,
    });
    fireEvent.click(screen.getByRole("button", { name: enMessages.session.rail_search_retry }));
    await advance(1_000);
    expect(fetchWorkspacesMock).toHaveBeenCalledTimes(2);
    expect(fetchSessionsMock).toHaveBeenLastCalledWith({ workspaceId: "ws_1" });
    expect(screen.getByText("rows")).toBeTruthy();
    expect(screen.queryByText(enMessages.session.rail_load_stalled_spaces)).toBeNull();
  });

  it("a wait that ended starts over with the full skeleton time instead of the old error", async () => {
    const view = renderList();
    await advance(SESSION_LIST_WAIT_MS + 1_000);
    expect(screen.getByText(enMessages.session.rail_load_stalled_spaces)).toBeTruthy();
    // The drawer closes (its list is disabled) and reopens for the same scope.
    view.rerender(tree(false));
    await advance(100);
    view.rerender(tree(true));
    await advance(100);
    expect(screen.queryByText(enMessages.session.rail_load_stalled_spaces)).toBeNull();
    expect(screen.getByRole("status", { name: enMessages.session.rail_loading })).toBeTruthy();
    await advance(SESSION_LIST_WAIT_MS);
    expect(screen.getByText(enMessages.session.rail_load_stalled_spaces)).toBeTruthy();
  });
});

describe("session list while /sessions never answers (the Space list did)", () => {
  type Deferred = { resolve: (value: unknown) => void };
  let calls: Deferred[];

  beforeEach(() => {
    vi.useFakeTimers();
    vi.stubEnv("NEXT_PUBLIC_BOWHEAD_SSE_MODE", "sse");
    sessionsResource.reset();
    spacesResource.reset();
    Object.defineProperty(window, "localStorage", { value: memoryStorage(), configurable: true });
    Object.defineProperty(window, "sessionStorage", { value: memoryStorage(), configurable: true });
    fetchWorkspacesMock.mockReset().mockResolvedValue({
      items: [{ id: "ws_1", name: "East", desc: "", role: "owner" }],
      active_id: "ws_1",
    });
    calls = [];
    fetchSessionsMock.mockReset().mockImplementation(() => new Promise((resolve) => calls.push({ resolve })));
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllEnvs();
    sessionsResource.reset();
    spacesResource.reset();
  });

  it("names the session list as what is pending, and a late answer fills the list without Retry", async () => {
    renderList();
    await advance(1_000);
    expect(calls.length).toBeGreaterThan(0);
    await advance(SESSION_LIST_WAIT_MS);
    expect(screen.getByText(enMessages.session.rail_load_stalled_sessions)).toBeTruthy();
    expect(screen.queryByText(enMessages.session.rail_load_stalled_spaces)).toBeNull();

    await act(async () => {
      for (const call of calls) {
        call.resolve({
          groups: [{ id: "today", label: "TODAY", items: [{ id: "s_1", title: "Q3", state: "done" }] }],
          archived_count: 0,
        });
      }
      await vi.advanceTimersByTimeAsync(10);
    });
    expect(screen.getByText("rows")).toBeTruthy();
    expect(screen.queryByText(enMessages.session.rail_load_stalled_sessions)).toBeNull();
  });
});
