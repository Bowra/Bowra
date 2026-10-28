/**
 * Request ownership for the two lists several surfaces render at once.
 *
 * `/auth/me/workspaces` used to be fetched by every mounted `useSpace()` (the
 * home page + the topbar picker = 2 on a cold home load), and `/sessions` by the
 * sidebar, the mobile drawer and the workspace page independently. Both now have
 * a single owner; these tests fail if a component goes back to owning its own
 * request.
 *
 * SSE mode is stubbed to "sse" before importing `use-sessions`, because the
 * gate/mock profile serves the list from fixtures and never calls the API.
 */
import { render, waitFor } from "@testing-library/react";
import { useEffect, useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/api", () => ({
  fetchSessions: vi.fn(async () => ({ groups: [], archived_count: 0 })),
  fetchWorkspaces: vi.fn(async () => ({
    items: [{ id: "ws_a", name: "Space A", desc: "" }],
    active_id: "ws_a",
  })),
  setLastWorkspace: vi.fn(async () => {}),
}));

vi.stubEnv("NEXT_PUBLIC_BOWHEAD_SSE_MODE", "sse");

const { fetchSessions, fetchWorkspaces } = await import("@/lib/api");
const { useSessionList, sessionsResource } = await import("@/lib/use-sessions");
const { useSpaceSelection, spacesResource } = await import("@/lib/use-space");

beforeEach(() => {
  sessionsResource.reset();
  spacesResource.reset();
  vi.mocked(fetchSessions).mockClear();
  vi.mocked(fetchWorkspaces).mockClear();
  // jsdom's storage stub here exposes get/setItem only (see lib/space-storage).
  window.localStorage?.removeItem?.("bowhead.space_id");
  window.localStorage?.removeItem?.("bowhead.workspace_id");
});

describe("space list ownership", () => {
  it("serves several space consumers from one request", async () => {
    const names: string[] = [];
    function Consumer({ label }: { label: string }) {
      const space = useSpaceSelection();
      if (space.activeId) names.push(`${label}:${space.activeId}`);
      return null;
    }

    render(
      <>
        <Consumer label="page" />
        <Consumer label="picker" />
        <Consumer label="settings" />
      </>,
    );

    await waitFor(() => expect(names).toContain("page:ws_a"));
    expect(names).toContain("picker:ws_a");
    expect(names).toContain("settings:ws_a");
    expect(fetchWorkspaces).toHaveBeenCalledTimes(1);
  });
});

describe("session list ownership", () => {
  it("serves sidebar, drawer and page from one request", async () => {
    function Sidebar() {
      // No explicit space: follows the active one, like the real sidebar.
      useSessionList(0, true);
      return null;
    }
    function Drawer() {
      useSessionList(0, true);
      return null;
    }
    function Page() {
      // The workspace page renders "" for the frame before the space list
      // resolves, then the real id — both must land on the shared entry.
      const [id, setId] = useState("");
      useEffect(() => {
        const timer = setTimeout(() => setId("ws_a"), 5);
        return () => clearTimeout(timer);
      }, []);
      useSessionList(0, true, id);
      return null;
    }

    render(
      <>
        <Sidebar />
        <Drawer />
        <Page />
      </>,
    );

    await waitFor(() => expect(fetchSessions).toHaveBeenCalled());
    // Let the space list land so the implicit consumers resolve their key too.
    await waitFor(() =>
      expect(vi.mocked(fetchWorkspaces).mock.calls.length).toBe(1),
    );
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(vi.mocked(fetchSessions).mock.calls.length).toBe(1);
    expect(vi.mocked(fetchSessions).mock.calls[0]?.[0]).toEqual({
      workspaceId: "ws_a",
    });
  });

  it("does not fetch while every consumer is disabled", async () => {
    function Hidden() {
      useSessionList(0, false);
      return null;
    }
    render(<Hidden />);
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(fetchSessions).not.toHaveBeenCalled();
  });

  it("reports a disabled consumer as not loading", async () => {
    // The mobile drawer disables its own hook whenever the page supplies the
    // list. If "disabled" read as "loading", the drawer would sit on its
    // loading row forever for an account whose supplied list is empty.
    let observed: { loading: boolean; error: boolean } | null = null;
    function Disabled() {
      const state = useSessionList(0, false);
      observed = { loading: state.loading, error: state.error };
      return null;
    }
    render(<Disabled />);
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(observed).toEqual({ loading: false, error: false });
  });
});
