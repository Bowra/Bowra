import { expect, it } from "vitest";
import { maySelectCoachOperation, type CoachOperationIntent } from "./coach-operation-selection";
import type { CoachOperationOpen } from "./coach-operations";

const pointer: CoachOperationOpen = { source: "live", requestId: "b".repeat(32), turnId: "t_1", userId: "u_1", teamId: "team_1", workspaceId: "ws_1", sessionId: "s_1", reference: { kind: "eval_cases", agent_id: "osa_1", proposal_id: "acp_b" } };
const intent: CoachOperationIntent = { scope: "owner/team/space", generation: 4, sessionId: "s_1" };
const state = { intent, scope: intent.scope, generation: 4, selectedKey: "original_read_only_receipt", available: true };

it("selects the requested new receipt only for the accepted unchanged request", () => {
  expect(maySelectCoachOperation(pointer, state)).toBe(true);
  expect(maySelectCoachOperation(pointer, { ...state, intent: undefined })).toBe(false);
  expect(maySelectCoachOperation({ ...pointer, source: "replay" }, state)).toBe(false);
});

it("preserves later user intent even when the user closed the previous result", () => {
  expect(maySelectCoachOperation(pointer, { ...state, generation: 5 })).toBe(false);
  expect(maySelectCoachOperation(pointer, { ...state, generation: 5, selectedKey: null })).toBe(false);
  expect(maySelectCoachOperation(pointer, { ...state, available: false })).toBe(false);
});

it("rejects stale request scope and retains empty-workbench replay restoration", () => {
  expect(maySelectCoachOperation(pointer, { ...state, scope: "another_owner" })).toBe(false);
  expect(maySelectCoachOperation({ ...pointer, sessionId: "s_other" }, state)).toBe(false);
  expect(maySelectCoachOperation({ ...pointer, source: "replay" }, { ...state, intent: undefined, selectedKey: null })).toBe(true);
});
