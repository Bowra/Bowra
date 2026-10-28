import { afterEach, describe, expect, it, vi } from "vitest";

import {
  createWorkspace,
  fetchWorkspaces,
  updateWorkspace,
} from "@/lib/api";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("canonical Bowhead workspace routes", () => {
  it("keeps the frozen /auth/me/workspaces protocol spelling", async () => {
    const fetchMock = vi.fn(
      async (_input: RequestInfo | URL, init?: RequestInit) => {
        if (init?.method === "POST") {
          return new Response(
            JSON.stringify({ workspace_id: "ws_new" }),
            { status: 200 },
          );
        }
        if (init?.method === "PATCH") {
          return new Response(null, { status: 204 });
        }
        return new Response(
          JSON.stringify({
            items: [
              {
                workspace_id: "ws_1",
                name: "Space 1",
                description: "Test",
              },
            ],
            active_id: "ws_1",
          }),
          { status: 200 },
        );
      },
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(fetchWorkspaces()).resolves.toMatchObject({
      items: [{ id: "ws_1", name: "Space 1" }],
      active_id: "ws_1",
    });
    await createWorkspace("New space");
    await updateWorkspace("ws_1", { name: "Renamed" });

    expect(
      fetchMock.mock.calls.map(([input]) => String(input)),
    ).toEqual([
      "/api/v1/auth/me/workspaces",
      "/api/v1/auth/me/workspaces",
      "/api/v1/auth/me/workspaces/ws_1",
    ]);
  });
});
