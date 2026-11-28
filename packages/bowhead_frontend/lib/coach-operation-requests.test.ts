// @vitest-environment node
import { afterEach, expect, it, vi } from "vitest";

import { recordedChangePointer, requestCoachOperationOpen, subscribeCoachOperationRequests } from "./coach-operation-requests";
import { dispatchToTurnStore } from "./turn-store-dispatch";

const reference = { kind: "agent_change" as const, agent_id: "osa_1", proposal_id: "acp_recorded" };
const SCOPE = { userId: "u_1", teamId: "team_1", workspaceId: "ws_1", sessionId: "s_1" };
const listener = vi.fn();
const unsubscribe = subscribeCoachOperationRequests(listener);
afterEach(() => listener.mockClear());

it("passes an open-event request or an exact agent_change reference to the page, nothing else", () => {
  expect(requestCoachOperationOpen({ turnId: "t_1", requestId: "a".repeat(32) })).toBe(true);
  expect(requestCoachOperationOpen({ turnId: "t_1", reference })).toBe(true);
  expect(requestCoachOperationOpen({ turnId: "t_1", requestId: "../other" })).toBe(false);
  expect(requestCoachOperationOpen({ turnId: "t_1", reference: { ...reference, kind: "eval_cases" } as never })).toBe(false);
  expect(requestCoachOperationOpen({ turnId: "t_1", reference: { ...reference, recover: true } as never })).toBe(false);
  expect(requestCoachOperationOpen({ turnId: "", reference })).toBe(false);
  expect(listener.mock.calls.map(([request]) => request)).toEqual([
    { turnId: "t_1", requestId: "a".repeat(32) },
    { turnId: "t_1", reference },
  ]);
});

it("opens a recorded receipt only for a reference that Turn projected, in the current scope", () => {
  dispatchToTurnStore("tool_result", { result_meta: { coach_operations: { schema_version: 1, truncated: false, receipts: [
    { call_id: "call_1", tool_name: "agent_change_propose", operation_ref: reference, reported_ok: true },
  ] } } }, "replay", "t_recorded");
  const pointer = recordedChangePointer({ turnId: "t_recorded", reference }, SCOPE);
  expect(pointer).toMatchObject({ turnId: "t_recorded", ...SCOPE, reference });
  expect(pointer?.requestId).toBeTruthy();
  expect(recordedChangePointer({ turnId: "t_other", reference }, SCOPE)).toBeNull();
  expect(recordedChangePointer({ turnId: "t_recorded", reference: { ...reference, proposal_id: "acp_forged" } }, SCOPE)).toBeNull();
  expect(recordedChangePointer({ turnId: "t_recorded", reference }, null)).toBeNull();
  unsubscribe();
});
