import type { AgentEvalProgress, AgentEvalUsageRange } from "./agents-api";
import { apiFetch, readApiError } from "./api-client";

export interface CoachCIDelegation {
  delegation_id: string;
  agent_id: string;
  proposal_id: string;
  workspace_id: string;
  accepted_baseline: { working_copy_id: string; working_copy_version: number };
  state: "requested" | "authorized" | "revoked" | "exhausted" | "unknown";
  row_version: number;
  expires_at: string | null;
  current_admission: string | null;
  stop_reason: string | null;
  budget: { max_provider_calls: number; max_comparisons: number; duration_seconds: number };
  remaining_provider_calls: number;
  executions: Array<{ id: string; status: string; run_kind: string; operation_id: string; progress?: AgentEvalProgress }>;
  usage: Array<{ state: string; count: number; tokens_input: number; tokens_output: number }>;
  /** Consent estimate; present (possibly null) only when run insights are enabled. */
  estimate?: CoachCIEstimate | null;
}

export interface CoachCIEstimate {
  schema_version: 1;
  status: "available" | "unavailable";
  unavailable_reason: "insufficient_history" | "plan_unavailable" | "plan_identity_changed" | null;
  per_comparison: {
    tokens: AgentEvalUsageRange | null;
    tokens_cached: AgentEvalUsageRange | null;
    model_calls: AgentEvalUsageRange | null;
    duration_ms: AgentEvalUsageRange | null;
  } | null;
  max_comparisons: number;
  model_call_cap: number;
  tokens_ceiling_at_cap: number | null;
  may_exhaust_call_cap: boolean | null;
  basis: { window_days: number; scope: "same_cases" | "same_suites" | "agent"; sample_attempts: number } | null;
}

export interface CoachCIPage {
  items: CoachCIDelegation[];
  next_offset: number | null;
}

async function request<T>(agentId: string, suffix = "", body?: object): Promise<T> {
  const response = await apiFetch(`/api/v1/agents/${encodeURIComponent(agentId)}/coach-ci${suffix}`, {
    method: body === undefined ? "GET" : "POST",
    ...(body === undefined ? {} : { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  });
  if (!response.ok) throw await readApiError(response);
  return response.json() as Promise<T>;
}

export const listCoachCI = (agentId: string, offset = 0) => request<CoachCIPage>(agentId, `?offset=${offset}`);
export const authorizeCoachCI = (agentId: string, id: string, expectedVersion: number) =>
  request<CoachCIDelegation>(agentId, `/${encodeURIComponent(id)}/authorize`, { expected_version: expectedVersion, confirm_budget: true });
export const startCoachCI = (agentId: string, id: string) =>
  request<{ delegation: CoachCIDelegation; execution_requested: boolean; replayed: boolean }>(agentId, `/${encodeURIComponent(id)}/start`, {});
export const cancelCoachCI = (agentId: string, id: string) => request<CoachCIDelegation>(agentId, `/${encodeURIComponent(id)}/cancel`, {});
