import { afterEach, expect, it, vi } from "vitest";
import { apiFetch } from "./api-client";
import {
  coachOperationOpens,
  coachCaseEffects,
  coachOperationsVersion,
  parseCoachOperationReference,
  readCoachOperation,
} from "./coach-operations";
import { dispatchToTurnStore } from "./turn-store-dispatch";

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
