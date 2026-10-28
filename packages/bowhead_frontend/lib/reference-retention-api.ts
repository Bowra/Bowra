import { ApiError, apiJson } from "./api-client";

export interface ReferenceRetentionPolicy {
  days: number;
  version: number;
}

const URL = "/api/v1/team-settings/reference-retention";

function policyOf(value: unknown): ReferenceRetentionPolicy {
  const policy = value as Partial<ReferenceRetentionPolicy> | null;
  if (!policy || !Number.isSafeInteger(policy.days) || !Number.isSafeInteger(policy.version)
    || policy.days! < 1 || policy.days! > 180 || policy.version! < 0) {
    throw new ApiError(502, "Invalid retention policy response", { code: "reference_retention_response_invalid" });
  }
  return { days: policy.days!, version: policy.version! };
}

export async function getReferenceRetention(signal?: AbortSignal): Promise<ReferenceRetentionPolicy> {
  return policyOf(await apiJson<unknown>(URL, { cache: "no-store", signal }));
}

export async function setReferenceRetention(days: number, expectedVersion: number, signal?: AbortSignal): Promise<ReferenceRetentionPolicy> {
  const result = policyOf(await apiJson<unknown>(URL, {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ days, expected_version: expectedVersion }), signal,
  }));
  if (result.days !== days || result.version !== expectedVersion + 1) {
    throw new ApiError(502, "Unexpected retention policy acknowledgement", { code: "reference_retention_response_invalid" });
  }
  return result;
}
