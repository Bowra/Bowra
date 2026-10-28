import { beforeEach, expect, it, vi } from "vitest";
import { apiFetch } from "./api-client";
import { readCoachResourceEditor, type CoachResourceOpen } from "./coach-resource-editors";

vi.mock("./api-client", () => ({ apiFetch: vi.fn(), readApiError: vi.fn() }));
const pointer: CoachResourceOpen = { requestId: "a".repeat(32), turnId: "t_original", sessionId: "s_original", workspaceId: "ws_original", userId: "u_owner", teamId: "team_original",
  reference: { kind: "capability", id: "tcap_original", revision: "1" }, parent: { kind: "agent_draft", id: "sad_original", version: 2 } };
const value = { ok: true, schema_version: 1, editor_kind: "plugin", reference: { ...pointer.reference, revision: "2" }, parent: pointer.parent, title: "Original Plugin", read_only: true,
  target: { kind: "plugin", plugin: { id: "tcap_original", team_id: "team_original", row_version: 3, revision: 2, status: "active", name: "Original Plugin", description: "Same identity", tool_refs: [], skill_grants: [] } } };
beforeEach(() => vi.clearAllMocks());

it("reads the current same Plugin identity with the original parent and scope", async () => {
  vi.mocked(apiFetch).mockResolvedValue(new Response(JSON.stringify(value), { status: 200 }));
  const read = await readCoachResourceEditor(pointer, new AbortController().signal);
  expect(read).toEqual(value);
  const init = vi.mocked(apiFetch).mock.calls[0][1]!;
  expect(init.cache).toBe("no-store");
  expect(JSON.parse(init.body as string)).toEqual({ reference: pointer.reference, parent: pointer.parent, workspace_id: pointer.workspaceId, session_id: pointer.sessionId });
});

it.each([{ team_id: "team_other" }, { id: "tcap_replaced" }, { revision: 1 }, { row_version: 0 }])("rejects a mismatched Plugin projection: %s", async (patch) => {
  vi.mocked(apiFetch).mockResolvedValue(new Response(JSON.stringify({ ...value, target: { ...value.target, plugin: { ...value.target.plugin, ...patch } } }), { status: 200 }));
  await expect(readCoachResourceEditor(pointer, new AbortController().signal)).rejects.toThrow("Invalid Plugin editor projection");
});
