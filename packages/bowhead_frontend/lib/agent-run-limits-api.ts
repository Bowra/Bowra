import { ApiError, apiJson } from "./api-client";

/** Team agent run limits (`/api/v1/team-settings/agent-run-limits`). `stepLimit: null` means unlimited. */
export interface AgentRunLimits {
  turnTimeLimitS: number;
  stepLimit: number | null;
  toolCallLimit: number;
  modelCallTimeLimitS?: number | null;
  wrapUpReserveS: number;
  isDefault: boolean;
  version: string;
  defaults: { turnTimeLimitS: number; stepLimit: number | null; toolCallLimit: number; modelCallTimeLimitS?: number | null };
  ranges: {
    turnTimeLimitS: { min: number; max: number };
    stepLimit: { min: number; max: number };
    toolCallLimit: { min: number; max: number };
    modelCallTimeLimitS?: { min: number; max: number };
  };
}

export interface AgentRunLimitsDraft {
  turnTimeLimitS: number;
  stepLimit: number | null;
  toolCallLimit: number;
  modelCallTimeLimitS?: number | null;
}

const URL = "/api/v1/team-settings/agent-run-limits";

function invalid(): never {
  throw new ApiError(502, "Invalid agent run limits response", { code: "agent_run_limits_response_invalid" });
}

function int(value: unknown): number {
  return Number.isSafeInteger(value) ? (value as number) : invalid();
}

function range(value: unknown): { min: number; max: number } {
  const r = value as { min?: unknown; max?: unknown } | null;
  if (!r || typeof r !== "object") invalid();
  return { min: int(r.min), max: int(r.max) };
}

function limitsOf(value: unknown): AgentRunLimits {
  const v = value as Record<string, unknown> | null;
  if (!v || typeof v !== "object" || typeof v.version !== "string" || typeof v.is_default !== "boolean") invalid();
  const defaults = v.defaults as Record<string, unknown> | null;
  const ranges = v.ranges as Record<string, unknown> | null;
  if (!defaults || !ranges) invalid();
  return {
    turnTimeLimitS: int(v.turn_time_limit_s),
    stepLimit: v.step_limit === null ? null : int(v.step_limit),
    toolCallLimit: int(v.tool_call_limit),
    ...(v.model_call_time_limit_s !== undefined ? { modelCallTimeLimitS: v.model_call_time_limit_s === null ? null : int(v.model_call_time_limit_s) } : {}),
    wrapUpReserveS: int(v.wrap_up_reserve_s),
    isDefault: v.is_default,
    version: v.version,
    defaults: {
      turnTimeLimitS: int(defaults.turn_time_limit_s),
      stepLimit: defaults.step_limit === null ? null : int(defaults.step_limit),
      toolCallLimit: int(defaults.tool_call_limit),
      ...(defaults.model_call_time_limit_s !== undefined ? { modelCallTimeLimitS: defaults.model_call_time_limit_s === null ? null : int(defaults.model_call_time_limit_s) } : {}),
    },
    ranges: {
      turnTimeLimitS: range(ranges.turn_time_limit_s),
      stepLimit: range(ranges.step_limit),
      toolCallLimit: range(ranges.tool_call_limit),
      ...(ranges.model_call_time_limit_s !== undefined ? { modelCallTimeLimitS: range(ranges.model_call_time_limit_s) } : {}),
    },
  };
}

/** The Team's limits, or `null` when the feature is off (404) or the user is not a Team admin (403). */
export async function getAgentRunLimits(signal?: AbortSignal): Promise<AgentRunLimits | null> {
  try {
    return limitsOf(await apiJson<unknown>(URL, { cache: "no-store", signal }));
  } catch (caught) {
    if (caught instanceof ApiError && (caught.status === 404 || caught.status === 403)) return null;
    throw caught;
  }
}

export async function saveAgentRunLimits(
  draft: AgentRunLimitsDraft,
  expectedVersion: string,
  signal?: AbortSignal,
): Promise<AgentRunLimits> {
  return limitsOf(
    await apiJson<unknown>(URL, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        turn_time_limit_s: draft.turnTimeLimitS,
        step_limit: draft.stepLimit,
        tool_call_limit: draft.toolCallLimit,
        ...(draft.modelCallTimeLimitS !== undefined ? { model_call_time_limit_s: draft.modelCallTimeLimitS } : {}),
        expected_version: expectedVersion,
      }),
      signal,
    }),
  );
}

export async function resetAgentRunLimits(signal?: AbortSignal): Promise<AgentRunLimits> {
  return limitsOf(await apiJson<unknown>(URL, { method: "DELETE", signal }));
}
