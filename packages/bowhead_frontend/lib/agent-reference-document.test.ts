import { beforeEach, describe, expect, it, vi } from "vitest";

import { resolveAgentReferenceDocument } from "./agent-reference-document";
import { getReferenceCatalog } from "./reference-catalog-api";

vi.mock("./reference-catalog-api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./reference-catalog-api")>()),
  getReferenceCatalog: vi.fn(),
}));

const SCOPE = { userId: "u_1", teamId: "team_1", workspaceId: "ws_1", requestedSessionId: "s_1", authSessionGeneration: "g1" };
const COACH = { kind: "agent", source_kind: "system", id: "system:agent-coach", version: "sha256:" + "a".repeat(64) } as const;

function page(items: unknown[], next: string | null = null) {
  return { catalog_generation: "c1", input_schema_versions: [1], entry_support: [], groups: [{ id: "agents", items, error: null }], next_cursor: next } as never;
}

function coach(change: Record<string, unknown> = {}) {
  return { kind: "agent", source_kind: "system", id: "system:agent-coach", label: "Coach", summary: "", resource: COACH, availability: "baseline", reason_code: null, ...change };
}

describe("resolveAgentReferenceDocument", () => {
  beforeEach(() => vi.mocked(getReferenceCatalog).mockReset());

  it("builds @Agent plus the message from the catalog's exact reference, found by id after a renamed label", async () => {
    vi.mocked(getReferenceCatalog)
      .mockResolvedValueOnce(page([]))
      .mockResolvedValueOnce(page([], "next"))
      .mockResolvedValueOnce(page([coach({ label: "Team Coach" })]));
    const document = await resolveAgentReferenceDocument(SCOPE, { agentId: "system:agent-coach", label: "Coach", text: "I’ve marked the answers." }, new AbortController().signal);
    expect(document?.nodes).toEqual([
      expect.objectContaining({ type: "reference", resource: COACH, label: "Team Coach", parameters: {} }),
      { type: "text", text: " I’ve marked the answers." },
    ]);
    expect(vi.mocked(getReferenceCatalog).mock.calls.map((call) => [call[1].query, call[3]])).toEqual([["Coach", null], ["", null], ["", "next"]]);
  });

  it("returns nothing for an Agent the Owner can't use now", async () => {
    vi.mocked(getReferenceCatalog).mockResolvedValue(page([coach({ availability: "unavailable", resource: null })]));
    expect(await resolveAgentReferenceDocument(SCOPE, { agentId: "system:agent-coach", label: "Coach", text: "x" }, new AbortController().signal)).toBeNull();
  });
});
