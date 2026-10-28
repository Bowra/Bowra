import { describe, expect, it, vi } from "vitest";

const apiJson = vi.fn();
vi.mock("./api-client", () => ({ apiJson: (...args: unknown[]) => apiJson(...args) }));

import { fetchHomeProposals, fetchHomeSnapshot } from "./home-api";

describe("home API parse boundary", () => {
  it("turns an empty or partial proposals response into an empty list instead of undefined", async () => {
    apiJson.mockResolvedValueOnce({});
    expect((await fetchHomeProposals("ws_1", "zh-CN")).items).toEqual([]);
    apiJson.mockResolvedValueOnce({ items: [null, { id: "p1", kind: "analyze", title: "T", reason: "R", prompt: "P" }, { id: "p2" }] });
    const result = await fetchHomeProposals("ws_1", "zh-CN");
    expect(result.items.map((item) => item.id)).toEqual(["p1"]);
    expect(result.strategy).toBe("deterministic");
  });

  it("fills missing snapshot lists, counts and scope so the home page renders empty sections", async () => {
    apiJson.mockResolvedValueOnce({ outputs: [{ id: "o1", title: "Q2" }] });
    const snapshot = await fetchHomeSnapshot("ws_1");
    expect(snapshot.attention).toEqual([]);
    expect(snapshot.active).toEqual([]);
    expect(snapshot.counts).toEqual({ attention: 0, active: 0, outputs: 1 });
    expect(snapshot.scope).toEqual({ workspaceId: "ws_1", workspaceName: "" });
    expect(snapshot.issues).toEqual([]);
  });
});
