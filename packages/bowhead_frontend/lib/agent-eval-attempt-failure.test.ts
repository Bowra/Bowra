import { describe, expect, it } from "vitest";
import { attemptFailureReason, attemptReasonDetail, retryCanHelp, storedAttemptFailure } from "./agent-eval-attempt-failure";

const t = (key: string, values?: Record<string, string>) => `${key}${values ? JSON.stringify(values) : ""}`;

describe("attempt failure rule", () => {
  it("offers a retry only when testing again can give a verdict", () => {
    expect(retryCanHelp(["attempt_agent_model_rate_limited"])).toBe(true);
    expect(retryCanHelp(["attempt_infrastructure_incomplete"])).toBe(true);
    expect(retryCanHelp(["attempt_cancelled"])).toBe(true);
    // Causes that stay: a missing model, rejected credentials, a Tool evaluation can't run.
    expect(retryCanHelp(["attempt_model_unavailable"])).toBe(false);
    expect(retryCanHelp(["run_model_unavailable", "required_attempts_missing"])).toBe(false);
    expect(retryCanHelp(["attempt_agent_model_auth_failed"])).toBe(false);
    expect(retryCanHelp(["attempt_judge_not_configured"])).toBe(false);
    expect(retryCanHelp(["required_tool_not_run_in_eval"])).toBe(false);
    // An answer that depended on a Tool evaluation can't run yet (a data query) can't be tested by retrying either.
    expect(retryCanHelp(["tool_not_run_in_eval"])).toBe(false);
    // One cause that can clear is enough for testing again to help.
    expect(retryCanHelp(["attempt_agent_model_auth_failed", "attempt_agent_model_timeout"])).toBe(true);
    // Reasons that are not attempt causes don't decide it.
    expect(retryCanHelp(["provider_identity_unknown"])).toBe(true);
  });

  it("gives raw run results the same reason as the server", () => {
    expect(attemptFailureReason("judge_unknown", "agent_eval_judge_unavailable")).toBe("attempt_judge_unavailable");
    expect(attemptFailureReason("judge_unknown", "agent_eval_judge_not_configured")).toBe("attempt_judge_not_configured");
    expect(attemptFailureReason("cancelled", "agent_eval_cancelled")).toBe("attempt_cancelled");
    expect(attemptFailureReason("infra_failed", "agent_eval_model_unavailable")).toBe("attempt_model_unavailable");
    expect(attemptFailureReason("infra_failed", "agent_eval_agent_model_timeout")).toBe("attempt_agent_model_timeout");
    expect(attemptFailureReason("infra_failed", "agent_eval_tool_failed")).toBe("attempt_tool_failed");
    expect(attemptFailureReason("infra_failed", "agent_eval_attempt_failed")).toBe("attempt_infrastructure_incomplete");
    expect(attemptFailureReason("infra_failed", "agent_eval_attempt_timeout")).toBeNull();
    expect(attemptFailureReason("infra_failed", "agent_eval_required_tool_not_run")).toBeNull();
    expect(attemptFailureReason("infra_failed", "agent_eval_tool_not_run_in_eval")).toBeNull();
    expect(attemptFailureReason("passed", null)).toBeNull();
  });

  it("names the cases, the Tool, the cancellation cause, the role and the reference", () => {
    const facts = { cases: "“Q1”", tools: [] as string[], causes: [] as (string | null)[], references: [] as string[] };
    expect(attemptReasonDetail("attempt_tool_failed", { ...facts, tools: ["python_exec"], toolNames: "“Run code”" }, t))
      .toBe('version_reason_detail_attempt_tool_failed{"tools":"“Run code”","cases":"“Q1”"}');
    expect(attemptReasonDetail("attempt_cancelled", { ...facts, causes: ["stopped", "restart"] }, t))
      .toBe('version_reason_detail_attempt_cancelled_restart{"cases":"“Q1”"}');
    expect(attemptReasonDetail("attempt_infrastructure_incomplete", { ...facts, references: ["aej_1", "aej_2"] }, t))
      .toBe('version_reason_detail_attempt_platform{"cases":"“Q1”","reference":"aej_1, aej_2"}');
    expect(attemptReasonDetail("attempt_infrastructure_incomplete", facts, t))
      .toBe('version_reason_detail_attempt_platform_unreferenced{"cases":"“Q1”"}');
    expect(attemptReasonDetail("attempt_model_unavailable", { ...facts, role: "Main model" }, t))
      .toBe('version_reason_detail_attempt_model_unavailable{"cases":"“Q1”","role":"Main model"}');
    // A run stopped before its attempts concerns the whole test, not particular cases.
    expect(attemptReasonDetail("run_model_unavailable", { ...facts, cases: "", role: "Main model" }, t))
      .toBe('version_reason_detail_run_model_unavailable{"role":"Main model"}');
    expect(attemptReasonDetail("flaky_limit_exceeded", facts, t)).toBeNull();
  });

  it("reads only the safe stored facts", () => {
    expect(storedAttemptFailure({ failure: { category: "tool", tool: "python_exec", reference: "aej_1", raw: "secret" } }))
      .toEqual({ cause: null, tool: "python_exec", reference: "aej_1", role: null, workspace: null, owner: null });
    expect(storedAttemptFailure({})).toEqual({ cause: null, tool: null, reference: null, role: null, workspace: null, owner: null });
  });

  it("names each live Datarc cause, and offers a retry only for the three retryable ones", () => {
    const prefix = "attempt_datarc_unavailable_";
    for (const cause of ["datarc_unreachable", "datarc_busy", "datarc_timeout"]) {
      expect(retryCanHelp([`${prefix}${cause}`])).toBe(true);
    }
    for (const cause of ["no_connection", "connection_unusable", "connection_rejected", "starter_not_owner", "owner_identity_unverified", "no_workspace", "owner_not_member", "owner_permission_denied"]) {
      expect(retryCanHelp([`${prefix}${cause}`])).toBe(false);
    }
    // Data that changed during the test is not compared; testing again can read stable data.
    expect(retryCanHelp(["data_changed_during_test"])).toBe(true);
    expect(attemptFailureReason("infra_failed", "agent_eval_datarc_unavailable", "datarc_busy")).toBe(`${prefix}datarc_busy`);
    expect(attemptFailureReason("infra_failed", "agent_eval_datarc_unavailable", null)).toBe("attempt_infrastructure_incomplete");
    const facts = { cases: "“Q1”", tools: [] as string[], causes: [] as (string | null)[], references: ["aej_1"] };
    expect(attemptReasonDetail(`${prefix}starter_not_owner`, { ...facts, owner: "Olivia" }, t)).toBe(
      `version_reason_detail_${prefix}starter_not_owner{"cases":"“Q1”","owner":"Olivia"}`,
    );
    expect(attemptReasonDetail(`${prefix}starter_not_owner`, facts, t)).toBe(`version_reason_detail_${prefix}starter_not_owner_unnamed{"cases":"“Q1”"}`);
    expect(attemptReasonDetail(`${prefix}owner_not_member`, { ...facts, workspace: "Retail" }, t)).toBe(
      `version_reason_detail_${prefix}owner_not_member{"cases":"“Q1”","workspace":"Retail"}`,
    );
    expect(attemptReasonDetail(`${prefix}datarc_unreachable`, facts, t)).toBe(`version_reason_detail_${prefix}datarc_unreachable{"cases":"“Q1”","reference":"aej_1"}`);
    expect(attemptReasonDetail(`${prefix}datarc_unreachable`, { ...facts, references: [] }, t)).toBe(`version_reason_detail_${prefix}datarc_unreachable_unreferenced{"cases":"“Q1”"}`);
    expect(storedAttemptFailure({ failure: { category: "data_connection", cause: "owner_not_member", workspace: "Retail", owner_name: "Olivia" } })).toMatchObject({
      cause: "owner_not_member",
      workspace: "Retail",
      owner: "Olivia",
    });
  });
});
