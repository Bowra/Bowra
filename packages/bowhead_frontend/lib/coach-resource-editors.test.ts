import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { parseResourceReference, projectCoachResourceOpen, coachResourceOpens, type CoachResourceOpen } from "./coach-resource-editors";
import { dispatchToTurnStore } from "./turn-store-dispatch";
import { agentEditorForOperationOpen, agentEditorsToReload, openAgentEditorIds, useCoachResourceEditors, type CoachResourceObject } from "./use-coach-resource-editors";
import type { CoachChangeEffect, CoachOperationOpen } from "./coach-operations";

const pointer = (id = "sk_first", requestId = "a".repeat(32)): CoachResourceOpen => ({ requestId,
  userId: "u_owner", teamId: "team_original", workspaceId: "ws_original", sessionId: "s_original", turnId: "t_resources",
  reference: { kind: "skill", source_kind: "user", id, revision: "sha256:" + "1".repeat(64) },
  parent: { kind: "agent_draft", id: "sad_parent", version: 2 } });

describe("original resource views", () => {
  it("projects the same pointer in live and replay without retaining arbitrary event content", () => {
    const original = pointer();
    const payload = { request_id: original.requestId, user_id: original.userId, team_id: original.teamId,
      workspace_id: original.workspaceId, session_id: original.sessionId, turn_id: original.turnId,
      reference: original.reference, parent: original.parent, secret: "must not be retained" };
    expect(dispatchToTurnStore("coach.resource_open", payload)).toBe(true);
    projectCoachResourceOpen(payload);
    expect(coachResourceOpens(original.turnId)).toEqual([original]);
    expect(parseResourceReference({ ...original.reference, credentials: "untrusted" })).toBeNull();
    projectCoachResourceOpen({ ...payload, request_id: "b".repeat(32), parent: { kind: "agent_draft", id: "../elsewhere", version: 1 } });
    expect(coachResourceOpens(original.turnId)).toHaveLength(1);
  });

  it("retains one original editor and parent across changed versions and background requests", () => {
    const { result } = renderHook(() => useCoachResourceEditors("original"));
    act(() => result.current.open(pointer(), true));
    const first = result.current.objects[0];
    act(() => result.current.open(pointer("sk_second", "b".repeat(32)), true));
    const second = result.current.objects[1];
    act(() => result.current.open({ ...pointer(), requestId: "c".repeat(32), parent: { kind: "agent_draft", id: "sad_other", version: 5 } }));
    expect(result.current.selectedKey).toBe(second.key);
    expect(result.current.objects[0]).toBe(first);
    expect(result.current.objects).toHaveLength(2);
    act(() => result.current.close(first.key, first.incarnation));
    act(() => result.current.open(pointer()));
    expect(result.current.objects).toHaveLength(1);
    act(() => result.current.open(pointer("sk_first", "d".repeat(32)), true));
    const reopened = result.current.objects[1];
    expect(reopened.incarnation).not.toBe(first.incarnation);
    act(() => result.current.close(first.key, first.incarnation));
    act(() => result.current.title(first.key, first.incarnation, "Late title"));
    expect(result.current.objects[1]).toBe(reopened);
  });

  it("clears private pointers on Chat or account changes", () => {
    const { result, rerender } = renderHook(({ scope }) => useCoachResourceEditors(scope), { initialProps: { scope: "original" } });
    act(() => result.current.open(pointer(), true));
    const first = result.current.objects[0];
    rerender({ scope: "another" });
    expect(result.current.objects).toEqual([]);
    act(() => result.current.open(pointer(), true));
    act(() => result.current.close(first.key, first.incarnation));
    expect(result.current.objects).toHaveLength(1);
  });

  it("reloads only the original object and fences callbacks from its discarded editor", () => {
    const { result } = renderHook(() => useCoachResourceEditors("original"));
    act(() => result.current.open(pointer(), true));
    const first = result.current.objects[0];
    act(() => result.current.open(pointer("sk_second", "b".repeat(32))));
    const sibling = result.current.objects[1];
    act(() => result.current.reload(first.key, first.incarnation));
    const refreshed = result.current.objects[0];
    expect(refreshed.pointer).toBe(first.pointer);
    expect(refreshed.incarnation).not.toBe(first.incarnation);
    expect(refreshed.editorRef).not.toBe(first.editorRef);
    expect(result.current.selectedKey).toBe(first.key);
    expect(result.current.objects[1]).toBe(sibling);
    act(() => result.current.close(first.key, first.incarnation));
    act(() => result.current.reload(first.key, first.incarnation));
    act(() => result.current.title(first.key, first.incarnation, "Late title"));
    expect(result.current.objects[0]).toBe(refreshed);
  });
});

describe("agentEditorsToReload", () => {
  const operation = (agentId: string, requestId: string): CoachOperationOpen => ({
    requestId, turnId: "t_1", userId: "u_1", teamId: "team_1", workspaceId: "ws_1", sessionId: "s_1",
    reference: { kind: "agent_change", agent_id: agentId, proposal_id: "acp_1" },
  });
  const editor = (agentId: string, dirty = false): CoachResourceObject => ({
    key: `agent:${agentId}`, incarnation: "i_1", title: "",
    pointer: { ...pointer(agentId), reference: { kind: "agent", id: agentId, revision: "current" } } as CoachResourceOpen,
    editorRef: { current: { requestClose: () => undefined, capture: () => ({ target: {} as never, dirty, pending_write: false }) } },
  });

  it("reloads the written Agent's clean editor once per receipt and keeps unsaved input", () => {
    const handled = new Set<string>();
    const clean = editor("osa_1");
    const other = editor("osa_2");
    expect(agentEditorsToReload([operation("osa_1", "r1")], [clean, other], handled)).toEqual([clean]);
    expect(agentEditorsToReload([operation("osa_1", "r1")], [clean, other], handled)).toEqual([]);
    expect(agentEditorsToReload([operation("osa_1", "r2")], [editor("osa_1", true)], handled)).toEqual([]);
  });

  it("also reloads for a write read from the run record, once per write whichever arrives first", () => {
    const effect = (agentId: string, proposalId = "acp_1"): CoachChangeEffect => ({
      turnId: "t_1", callId: "call_1", reportedOk: true, reference: { kind: "agent_change", agent_id: agentId, proposal_id: proposalId },
    });
    const clean = editor("osa_1");
    // Recorded first, then the same write's "open" event: one reload.
    const handled = new Set<string>();
    expect(agentEditorsToReload([], [clean], handled, [effect("osa_1")])).toEqual([clean]);
    expect(agentEditorsToReload([operation("osa_1", "r1")], [clean], handled, [effect("osa_1")])).toEqual([]);
    // Opened first, then recorded: one reload.
    const other = new Set<string>();
    expect(agentEditorsToReload([operation("osa_1", "r1")], [clean], other, [])).toEqual([clean]);
    expect(agentEditorsToReload([operation("osa_1", "r1")], [clean], other, [effect("osa_1")])).toEqual([]);
    // A new write reloads again; unsaved input is kept.
    expect(agentEditorsToReload([operation("osa_1", "r1")], [clean], other, [effect("osa_1"), effect("osa_1", "acp_2")])).toEqual([clean]);
    expect(agentEditorsToReload([], [editor("osa_1", true)], new Set(), [effect("osa_1", "acp_3")])).toEqual([]);
  });

  it("names the Agents whose hosted editor is open", () => {
    const skill = { ...editor("osa_x"), pointer: pointer("sk_1") };
    expect([...openAgentEditorIds([editor("osa_1"), skill])]).toEqual(["osa_1"]);
  });
});

describe("agentEditorForOperationOpen", () => {
  const open = (reference: CoachOperationOpen["reference"]): CoachOperationOpen => ({
    source: "live", requestId: "f".repeat(32), turnId: "t_1", userId: "u_1", teamId: "team_1", workspaceId: "ws_1", sessionId: "s_1", reference,
  });

  it("opens the Agent editor for Coach's draft write with the release bar on, in the open's own scope (F36)", () => {
    // Mutation: return null for `agent_change` → the old 「操作结果 · 智能体修改提案」 view opens again.
    const editorOpen = agentEditorForOperationOpen(open({ kind: "agent_change", agent_id: "osa_1", proposal_id: "acp_1" }), true);
    expect(editorOpen).toEqual({
      requestId: "f".repeat(32), turnId: "t_1", userId: "u_1", teamId: "team_1", workspaceId: "ws_1", sessionId: "s_1",
      reference: { kind: "agent", id: "osa_1", revision: "current" }, parent: null,
    });
    // The receipt then reads 已在右侧打开.
    const { result } = renderHook(() => useCoachResourceEditors("scope"));
    act(() => result.current.open(editorOpen!, true));
    expect([...openAgentEditorIds(result.current.objects)]).toEqual(["osa_1"]);
    expect(result.current.selectedKey).toBe(result.current.objects[0].key);
  });

  it("keeps the operation view with the switch off and for every other kind", () => {
    expect(agentEditorForOperationOpen(open({ kind: "agent_change", agent_id: "osa_1", proposal_id: "acp_1" }), false)).toBeNull();
    expect(agentEditorForOperationOpen(open({ kind: "eval_cases", agent_id: "osa_1", proposal_id: "acp_1" }), true)).toBeNull();
    expect(agentEditorForOperationOpen(open({ kind: "skill_save", operation_id: `csw_${"0".repeat(64)}` }), true)).toBeNull();
  });
});
