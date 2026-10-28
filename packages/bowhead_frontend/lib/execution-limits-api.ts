import { ApiError, apiJson } from "./api-client";

export interface ExecutionLimitsValues {
  sandbox_default_timeout_s: number;
  sandbox_max_timeout_s: number;
  html_max_bytes: number;
}

export interface ExecutionLimits extends ExecutionLimitsValues {
  defaults: ExecutionLimitsValues;
  ranges: Record<keyof ExecutionLimitsValues, { min: number; max: number }>;
  is_default: boolean;
  version: string;
}

const URL = "/api/v1/team-settings/execution-limits";
const fields = ["sandbox_default_timeout_s", "sandbox_max_timeout_s", "html_max_bytes"] as const;

function parse(value: unknown): ExecutionLimits {
  const v = value as ExecutionLimits | null;
  if (!v || typeof v.version !== "string" || typeof v.is_default !== "boolean"
    || fields.some((key) => !Number.isSafeInteger(v[key]) || v[key] < 1
      || !Number.isSafeInteger(v.defaults?.[key]) || v.defaults[key] < 1
      || !Number.isSafeInteger(v.ranges?.[key]?.min) || !Number.isSafeInteger(v.ranges?.[key]?.max)
      || v.ranges[key].min < 1 || v.ranges[key].max < v.ranges[key].min)) {
    throw new ApiError(502, "Invalid execution limits response", { code: "execution_limits_response_invalid" });
  }
  return v;
}

export async function getExecutionLimits(signal?: AbortSignal): Promise<ExecutionLimits> {
  return parse(await apiJson<unknown>(URL, { cache: "no-store", signal }));
}

/** Null restores inherited deployment defaults after the same version check as a save. */
export async function saveExecutionLimits(limits: ExecutionLimitsValues | null, expectedVersion: string, signal?: AbortSignal): Promise<ExecutionLimits> {
  return parse(await apiJson<unknown>(URL, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ limits, expected_version: expectedVersion }),
    signal,
  }));
}
