import { spaceHeader } from "./space-storage";
import { apiFetch, readApiError } from "./api-client";

export type CoachOperationReference =
  | { kind: "skill_save" | "plugin_save"; operation_id: string }
  | {
      kind: "tool_requirement";
      requirement_id: string;
      observation_id: string | null;
    }
  | {
      kind: "agent_change" | "eval_cases";
      agent_id: string;
      proposal_id: string;
    }
  | { kind: "ci_delegation"; agent_id: string; delegation_id: string }
  | { kind: "draft_record"; draft_id: string }
  | { kind: "draft_suggestion"; draft_id: string; suggestion_id: string };

export function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

/** Canonical pointers only; arbitrary extra fields cannot carry authority or content. */
export function parseCoachOperationReference(
  value: unknown,
): CoachOperationReference | null {
  const item = record(value);
  const matches = (key: string, pattern: RegExp) =>
    typeof item[key] === "string" && pattern.test(item[key] as string);
  const exact = (...keys: string[]) =>
    Object.keys(item).every((key) => ["kind", ...keys].includes(key));
  const agent = () => matches("agent_id", /^osa_[A-Za-z0-9_-]{1,156}$/);
  const draft = () => matches("draft_id", /^sad_[A-Za-z0-9_]{1,76}$/);
  switch (item.kind) {
    case "skill_save":
    case "plugin_save":
      if (
        exact("operation_id") &&
        matches(
          "operation_id",
          item.kind === "skill_save"
            ? /^csw_[0-9a-f]{64}$/
            : /^cpw_[0-9a-f]{64}$/,
        )
      )
        return { kind: item.kind, operation_id: item.operation_id as string };
      break;
    case "tool_requirement":
      if (
        exact("requirement_id", "observation_id") &&
        matches("requirement_id", /^ctrq_[0-9a-f]{32}$/) &&
        (item.observation_id == null ||
          matches("observation_id", /^ctro_[0-9a-f]{32}$/))
      )
        return {
          kind: item.kind,
          requirement_id: item.requirement_id as string,
          observation_id: (item.observation_id as string | undefined) ?? null,
        };
      break;
    case "agent_change":
    case "eval_cases":
      if (
        exact("agent_id", "proposal_id") &&
        agent() &&
        matches("proposal_id", /^acp_[A-Za-z0-9_-]{1,156}$/)
      )
        return {
          kind: item.kind,
          agent_id: item.agent_id as string,
          proposal_id: item.proposal_id as string,
        };
      break;
    case "ci_delegation":
      if (
        exact("agent_id", "delegation_id") &&
        agent() &&
        matches("delegation_id", /^acig_[0-9a-f]{32}$/)
      )
        return {
          kind: item.kind,
          agent_id: item.agent_id as string,
          delegation_id: item.delegation_id as string,
        };
      break;
    case "draft_record":
      if (exact("draft_id") && draft())
        return { kind: item.kind, draft_id: item.draft_id as string };
      break;
    case "draft_suggestion":
      if (
        exact("draft_id", "suggestion_id") &&
        draft() &&
        matches("suggestion_id", /^sas_[0-9a-f]{12}$/)
      )
        return {
          kind: item.kind,
          draft_id: item.draft_id as string,
          suggestion_id: item.suggestion_id as string,
        };
  }
  return null;
}

export type CoachOperationReceipt = {
  operation_ref: CoachOperationReference;
  domain_result: Record<string, unknown>;
};

export async function readCoachOperation(
  reference: CoachOperationReference,
  workspaceId: string,
  sessionId: string,
  signal: AbortSignal,
  teamId: string,
): Promise<CoachOperationReceipt> {
  const response = await apiFetch("/api/v1/agent-coach/operations/read", {
    method: "POST",
    cache: "no-store",
    signal,
    headers: {
      "Content-Type": "application/json",
      ...spaceHeader({ workspaceId, teamId }),
    },
    body: JSON.stringify({
      reference,
      workspace_id: workspaceId,
      session_id: sessionId,
    }),
  });
  if (!response.ok) throw await readApiError(response);
  const value = record(await response.json());
  const returned = parseCoachOperationReference(value.operation_ref);
  if (
    value.ok !== true ||
    value.schema_version !== 1 ||
    value.read_status !== "available" ||
    value.effects_applied_by_this_read !== false ||
    !returned ||
    JSON.stringify(returned) !== JSON.stringify(reference) ||
    !value.domain_result ||
    Array.isArray(value.domain_result) ||
    typeof value.domain_result !== "object"
  ) {
    throw new Error("Invalid original-operation receipt");
  }
  return {
    operation_ref: returned,
    domain_result: record(value.domain_result),
  };
}

export type CoachOperationOpen = {
  source?: "live" | "replay";
  requestId: string;
  turnId: string;
  userId: string;
  teamId: string;
  workspaceId: string;
  sessionId: string;
  reference: CoachOperationReference;
};
const opens = new Map<string, readonly CoachOperationOpen[]>();
const listeners = new Set<() => void>();
const EMPTY: readonly CoachOperationOpen[] = [];
let revision = 0;
export const subscribeCoachOperations = (listener: () => void) => {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
};
export const coachOperationsVersion = () => revision;
export const coachOperationOpens = (turnId: string | null) =>
  turnId ? (opens.get(turnId) ?? EMPTY) : EMPTY;
export function projectCoachOperationOpen(
  payload: Record<string, unknown>,
  source: "live" | "replay" = "live",
): void {
  const reference = parseCoachOperationReference(payload.reference);
  if (
    !reference ||
    typeof payload.request_id !== "string" ||
    !/^[0-9a-f]{32}$/.test(payload.request_id) ||
    !["turn_id", "user_id", "team_id", "workspace_id", "session_id"].every(
      (key) =>
        typeof payload[key] === "string" &&
        (payload[key] as string).length > 0 &&
        (payload[key] as string).length <= 256,
    )
  )
    return;
  const turnId = payload.turn_id as string;
  const current = opens.get(turnId) ?? EMPTY;
  if (current.some((item) => item.requestId === payload.request_id)) return;
  opens.set(turnId, [
    ...current.slice(-63),
    {
      reference,
      source,
      requestId: payload.request_id,
      turnId,
      userId: payload.user_id as string,
      teamId: payload.team_id as string,
      workspaceId: payload.workspace_id as string,
      sessionId: payload.session_id as string,
    },
  ]);
  if (opens.size > 256) opens.delete(opens.keys().next().value!);
  revision += 1;
  listeners.forEach((listener) => listener());
}


export type CoachCaseEffect = {
  turnId: string;
  callId: string;
  reference: { kind: "eval_cases"; agent_id: string; proposal_id: string };
  reportedOk: boolean;
};
const caseEffects = new Map<string, readonly CoachCaseEffect[]>();
const EMPTY_CASES: readonly CoachCaseEffect[] = [];
export const coachCaseEffects = (turnId: string | null) => turnId ? caseEffects.get(turnId) ?? EMPTY_CASES : EMPTY_CASES;

/** Discover original case effects separately from requests to open a view. */
export function projectCoachCaseEffects(payload: Record<string, unknown>, turnId: string | null): void {
  if (!turnId || turnId.length > 256) return;
  const meta = record(record(payload.result_meta).coach_operations);
  if (meta.schema_version !== 1 || typeof meta.truncated !== "boolean" || !Array.isArray(meta.receipts)
    || meta.receipts.length > 20 || Object.keys(meta).some((key) => !["schema_version", "receipts", "truncated"].includes(key))) return;
  const current = caseEffects.get(turnId) ?? EMPTY_CASES;
  const next = [...current];
  for (const raw of meta.receipts) {
    const item = record(raw);
    const reference = parseCoachOperationReference(item.operation_ref);
    if (item.tool_name !== "agent_eval_case_propose" || typeof item.call_id !== "string" || !item.call_id || item.call_id.length > 256
      || typeof item.reported_ok !== "boolean" || reference?.kind !== "eval_cases"
      || Object.keys(item).some((key) => !["call_id", "tool_name", "operation_ref", "reported_ok"].includes(key))) continue;
    if (next.some((entry) => entry.reference.agent_id === reference.agent_id && entry.reference.proposal_id === reference.proposal_id)) continue;
    next.push({ turnId, callId: item.call_id, reference: { ...reference, kind: "eval_cases" }, reportedOk: item.reported_ok });
  }
  if (next.length === current.length) return;
  caseEffects.set(turnId, next.slice(-20));
  if (caseEffects.size > 256) caseEffects.delete(caseEffects.keys().next().value!);
  revision += 1;
  listeners.forEach((listener) => listener());
}
