// @vitest-environment node
import { afterEach, expect, it, vi } from "vitest";
import { apiFetch } from "./api-client";
import {
  coachChangeEffects,
  coachChangeGroups,
  coachOperationOpens,
  coachCaseEffects,
  coachOperationsVersion,
  parseCoachOperationReference,
  readCoachOperation,
  uncoveredChangeOpens,
} from "./coach-operations";
import { dispatchToTurnStore } from "./turn-store-dispatch";

const changeMeta = (receipts: Array<{ call_id: string; agent_id: string; proposal_id: string }>) => ({
  result_meta: { coach_operations: { schema_version: 1, truncated: false, receipts: receipts.map((item) => ({
    call_id: item.call_id, tool_name: "agent_change_propose", reported_ok: true,
    operation_ref: { kind: "agent_change", agent_id: item.agent_id, proposal_id: item.proposal_id },
  })) } },
});

it("projects Coach's draft writes from the run record once across live, step_done and replay", () => {
  const payload = { card_id: "t_change", ...changeMeta([{ call_id: "call_a", agent_id: "osa_1", proposal_id: "acp_a" }]) };
  dispatchToTurnStore("tool_result", payload, "live", "t_change");
  dispatchToTurnStore("step_done", { ...payload, step_id: "tool_a", state: "success" }, "live", "t_change");
  dispatchToTurnStore("tool_result", payload, "replay", "t_change");
  expect(coachChangeEffects("t_change")).toEqual([{ turnId: "t_change", callId: "call_a", reportedOk: true, reference: { kind: "agent_change", agent_id: "osa_1", proposal_id: "acp_a" } }]);
  // Case receipts stay separate; another Tool's name or extra authority is not a draft write.
  expect(coachCaseEffects("t_change")).toHaveLength(0);
  const forged = { result_meta: { coach_operations: { schema_version: 1, truncated: false, receipts: [
    { call_id: "x", tool_name: "agent_eval_case_propose", reported_ok: true, operation_ref: { kind: "agent_change", agent_id: "osa_1", proposal_id: "acp_b" } },
    { call_id: "y", tool_name: "agent_change_propose", reported_ok: true, operation_ref: { kind: "agent_change", agent_id: "osa_1", proposal_id: "acp_c", recover: true } },
  ] } } };
  dispatchToTurnStore("tool_result", forged, "live", "t_change_forged");
  expect(coachChangeEffects("t_change_forged")).toHaveLength(0);
});

it("groups a Turn's writes by Agent and keeps only the open events the run record lacks", () => {
  dispatchToTurnStore("tool_result", changeMeta([
    { call_id: "call_1", agent_id: "osa_1", proposal_id: "acp_1" },
    { call_id: "call_2", agent_id: "osa_2", proposal_id: "acp_2" },
    { call_id: "call_3", agent_id: "osa_1", proposal_id: "acp_3" },
  ]), "live", "t_group");
  const effects = coachChangeEffects("t_group");
  expect(coachChangeGroups(effects)).toEqual([
    { agentId: "osa_1", references: [{ kind: "agent_change", agent_id: "osa_1", proposal_id: "acp_1" }, { kind: "agent_change", agent_id: "osa_1", proposal_id: "acp_3" }] },
    { agentId: "osa_2", references: [{ kind: "agent_change", agent_id: "osa_2", proposal_id: "acp_2" }] },
  ]);
  const open = (proposalId: string, requestId: string) => ({
    requestId, turnId: "t_group", userId: "u_1", teamId: "team_1", workspaceId: "ws_1", sessionId: "s_1",
    reference: { kind: "agent_change" as const, agent_id: "osa_1", proposal_id: proposalId },
  });
  expect(uncoveredChangeOpens([open("acp_1", "a".repeat(32)), open("acp_old", "b".repeat(32))], effects).map((item) => item.reference))
    .toEqual([{ kind: "agent_change", agent_id: "osa_1", proposal_id: "acp_old" }]);
});

vi.mock("./api-client", async (original) => ({
  ...(await original<typeof import("./api-client")>()),
  apiFetch: vi.fn(),
  readApiError: async () => new Error("Unavailable"),
}));
afterEach(() => vi.clearAllMocks());
const reference = {
  kind: "skill_save" as const,
  operation_id: `csw_${"a".repeat(64)}`,
};

it("projects exact replay once and rejects substituted references and extra authority", () => {
  const payload = {
    request_id: "b".repeat(32),
    turn_id: "t_operation_replay",
    user_id: "u_original",
    team_id: "team_original",
    workspace_id: "ws_original",
    session_id: "s_original",
    reference,
  };
  expect(dispatchToTurnStore("coach.operation_open", payload)).toBe(true);
  const opened = coachOperationOpens(payload.turn_id);
  expect(opened).toHaveLength(1);
  const revision = coachOperationsVersion();
  dispatchToTurnStore("coach.operation_open", {
    ...payload,
    reference: { kind: "draft_record", draft_id: "sad_other" },
  });
  expect(coachOperationsVersion()).toBe(revision);
  expect(coachOperationOpens(payload.turn_id)).toBe(opened);
  expect(opened[0].reference).toEqual(reference);
  for (const invalid of [
    { ...reference, recover: true },
    { ...reference, operation_id: "../outside" },
    { kind: "unknown" },
  ]) {
    expect(parseCoachOperationReference(invalid)).toBeNull();
  }
});

it("discovers one original case receipt from durable metadata across dual emission and replay", () => {
  const reference = { kind: "eval_cases", agent_id: "osa_1", proposal_id: "acp_saved" };
  const payload = { card_id: "t_case_effect", result_inline: null, result_meta: { coach_operations: { schema_version: 1, truncated: false, receipts: [{ call_id: "call_original", tool_name: "agent_eval_case_propose", operation_ref: reference, reported_ok: true }] } } };
  dispatchToTurnStore("tool_result", payload, "live", "t_case_effect");
  dispatchToTurnStore("step_done", { ...payload, step_id: "tool_original", state: "success" }, "live", "t_case_effect");
  dispatchToTurnStore("tool_result", payload, "replay", "t_case_effect");
  expect(coachCaseEffects("t_case_effect")).toEqual([{ turnId: "t_case_effect", callId: "call_original", reference, reportedOk: true }]);
  expect(coachOperationOpens("t_case_effect")).toHaveLength(0);
  const bad = { ...payload, result_meta: { coach_operations: { schema_version: 1, truncated: false, receipts: [{ call_id: "bad", tool_name: "agent_eval_case_propose", operation_ref: { ...reference, recover: true }, reported_ok: true }] } } };
  dispatchToTurnStore("tool_result", bad, "live", "t_case_bad");
  expect(coachCaseEffects("t_case_bad")).toHaveLength(0);
});

it("keeps view provenance without upgrading an already replayed request into new focus intent", () => {
  const payload = { turn_id: "t_source", request_id: "f".repeat(32), user_id: "u_1", team_id: "team_1", workspace_id: "ws_1", session_id: "s_1", reference };
  dispatchToTurnStore("coach.operation_open", payload, "replay");
  dispatchToTurnStore("coach.operation_open", payload, "live");
  expect(coachOperationOpens("t_source")[0].source).toBe("replay");
});

it("reads the original scope with no write or recovery options and verifies its receipt", async () => {
  const response = {
    ok: true,
    schema_version: 1,
    read_status: "available",
    effects_applied_by_this_read: false,
    operation_ref: reference,
    domain_result: { ok: false, state: "prepared", outcome: "unknown" },
  };
  vi.mocked(apiFetch).mockResolvedValueOnce(Response.json(response));
  const read = await readCoachOperation(
    reference,
    "ws_original",
    "s_original",
    new AbortController().signal,
    "team_original",
  );
  expect(read.domain_result.outcome).toBe("unknown");
  const [, options] = vi.mocked(apiFetch).mock.calls[0];
  expect(JSON.parse(options!.body as string)).toEqual({
    reference,
    workspace_id: "ws_original",
    session_id: "s_original",
  });
  expect(options?.cache).toBe("no-store");
  vi.mocked(apiFetch).mockResolvedValueOnce(
    Response.json({
      ...response,
      operation_ref: { ...reference, operation_id: `csw_${"b".repeat(64)}` },
    }),
  );
  await expect(
    readCoachOperation(
      reference,
      "ws_original",
      "s_original",
      new AbortController().signal,
      "team_original",
    ),
  ).rejects.toThrow("Invalid original-operation receipt");
});
