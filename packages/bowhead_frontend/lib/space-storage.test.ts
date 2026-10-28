import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { clearSpaceStorage, getActiveSpaceId, setActiveSpaceId, spaceHeader } from "./space-storage";
import { setActiveTeamId } from "./team-storage";
import { restoreActiveSpace, spacesResource, useSpaceSelection } from "./use-space";
import { setLastWorkspace } from "./api";

vi.mock("./api", () => ({
  fetchWorkspaces: vi.fn(async () => ({
    items: [{ id: "original", name: "Original" }, { id: "other", name: "Other" }],
    active_id: "other",
  })),
  setLastWorkspace: vi.fn(async () => {}),
}));
function storage() {
  const entries = new Map<string, string>();
  return {
    getItem: (key: string) => entries.get(key) ?? null,
    setItem: (key: string, value: string) => { entries.set(key, value); },
    removeItem: (key: string) => { entries.delete(key); },
  };
}
beforeEach(() => {
  vi.stubGlobal("localStorage", storage());
  vi.stubGlobal("sessionStorage", storage());
  spacesResource.reset();
  vi.mocked(setLastWorkspace).mockClear();
});
afterEach(() => vi.unstubAllGlobals());

it("keeps mounted and remounted readers on the original tab after another tab changes the preference", async () => {
  setActiveSpaceId("original");
  const first = renderHook(useSpaceSelection);
  await waitFor(() => expect(first.result.current.loading).toBe(false));
  localStorage.setItem("bowhead.space_id", "other");
  const remounted = renderHook(useSpaceSelection);
  expect(first.result.current.activeId).toBe("original");
  expect(remounted.result.current.activeId).toBe("original");
  expect(spaceHeader()["X-Space-Id"]).toBe("original");
});

it("restores the authorized Session for all current readers without saving a new preference", async () => {
  setActiveSpaceId("other");
  const first = renderHook(useSpaceSelection);
  const second = renderHook(useSpaceSelection);
  await waitFor(() => expect(first.result.current.loading).toBe(false));
  act(() => restoreActiveSpace("original"));
  expect(first.result.current.activeId).toBe("original");
  expect(second.result.current.activeId).toBe("original");
  expect(spaceHeader()["X-Space-Id"]).toBe("original");
  expect(localStorage.getItem("bowhead.space_id")).toBe("other");
  expect(setLastWorkspace).not.toHaveBeenCalled();
  act(() => first.result.current.setActiveId("other"));
  expect(second.result.current.activeId).toBe("other");
  expect(setLastWorkspace).toHaveBeenCalledExactlyOnceWith("other");
});

it("does not resurrect the previous Team or signed-out Space", () => {
  setActiveTeamId("team_a");
  setActiveSpaceId("original");
  setActiveTeamId("team_b");
  expect(getActiveSpaceId()).toBe("");
  setActiveSpaceId("other");
  clearSpaceStorage();
  expect(getActiveSpaceId()).toBe("");
});
