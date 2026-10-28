/**
 * ws-datarc-decouple P2b — team datarc connection admin API wrapper.
 *
 * Backend: /api/v1/datarc-connections/*  (team_admin gate; team-scoped → IDOR-safe).
 * The connection DTO never carries the secret — only `key_hint` (last 4).
 */

import {
  ApiError,
  apiFetch,
  apiJson,
  readApiError,
} from "./api-client";
import {
  normalizeErrorDiagnostics,
  parseErrorDiagnosticsWire,
  responseRequestId,
} from "./error-presentation";
import { genericUserFacingErrorMessage } from "./user-facing-error";

export type DatarcConnectionErrorCode =
  | "datarc_connection_auth_rejected"
  | "datarc_connection_configuration_invalid"
  | "datarc_connection_create_failed"
  | "datarc_connection_load_failed"
  | "datarc_connection_not_found"
  | "datarc_connection_secret_storage_unavailable"
  | "datarc_connection_store_unavailable"
  | "datarc_connection_unreachable"
  | "datarc_connection_update_failed"
  | "datarc_connection_url_rejected"
  | "datarc_connection_validation_failed";

const DATARC_CONNECTION_ERROR_CODES =
  new Set<DatarcConnectionErrorCode>([
    "datarc_connection_auth_rejected",
    "datarc_connection_configuration_invalid",
    "datarc_connection_create_failed",
    "datarc_connection_load_failed",
    "datarc_connection_not_found",
    "datarc_connection_secret_storage_unavailable",
    "datarc_connection_store_unavailable",
    "datarc_connection_unreachable",
    "datarc_connection_update_failed",
    "datarc_connection_url_rejected",
    "datarc_connection_validation_failed",
  ]);

export function isDatarcConnectionErrorCode(
  value: unknown,
): value is DatarcConnectionErrorCode {
  return (
    typeof value === "string" &&
    DATARC_CONNECTION_ERROR_CODES.has(value as DatarcConnectionErrorCode)
  );
}

export interface DatarcConnection {
  id: string;
  team_id: string;
  name: string;
  base_url: string;
  account_uid: string;
  key_hint: string;
  status: "active" | "archived";
  created_at: string;
  updated_at: string;
}

export interface CreateConnectionBody {
  name: string;
  base_url: string;
  auth_key: string;
  account_uid?: string;
  status?: "active" | "archived";
}

export interface ValidateBody {
  base_url: string;
  auth_key: string;
  account_uid?: string;
}

export interface ValidateResult {
  ok: boolean;
  error_code?: DatarcConnectionErrorCode;
  workspace_count?: number;
  project_count?: number;
  workspaces?: { id: string; name: string }[];
  projects?: { id: string; name: string }[];
  // embed-via-openapi: advisory probe — whether this key can use the datarc embed proxy
  // (powers concept-resolve dictionary lookups). Absent on older gateways.
  embed?: { available: boolean };
}

const BASE = "/api/v1/datarc-connections";

export async function listConnections(
  includeArchived = false,
): Promise<DatarcConnection[]> {
  const q = includeArchived ? "?include_archived=true" : "";
  const body = await apiJson<{ items: DatarcConnection[] }>(`${BASE}${q}`);
  return body.items;
}

export async function createConnection(
  body: CreateConnectionBody,
): Promise<DatarcConnection> {
  return apiJson<DatarcConnection>(BASE, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function rotateConnectionKey(
  id: string,
  authKey: string,
): Promise<DatarcConnection> {
  return apiJson<DatarcConnection>(`${BASE}/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ auth_key: authKey }),
  });
}

export async function setConnectionStatus(
  id: string,
  status: "active" | "archived",
): Promise<DatarcConnection> {
  return apiJson<DatarcConnection>(`${BASE}/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status }),
  });
}

export async function validateConnection(
  body: ValidateBody,
): Promise<ValidateResult> {
  const response = await apiFetch(`${BASE}/validate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw await readApiError(response);

  const result = await response.json() as ValidateResult & {
    error_code?: unknown;
    diagnostics?: unknown;
  };
  if (result.ok) return result;

  const code = isDatarcConnectionErrorCode(result.error_code)
    ? result.error_code
    : "datarc_connection_validation_failed";
  const parsed = parseErrorDiagnosticsWire(result.diagnostics);
  const requestId = responseRequestId(response);
  const diagnostics =
    parsed?.disclosure === "admin"
      ? normalizeErrorDiagnostics({
          ...parsed,
          code,
          ...(requestId ? { requestId } : {}),
          observedAt: new Date().toISOString(),
        })
      : undefined;
  throw new ApiError(response.status, genericUserFacingErrorMessage(), {
    body: result,
    code,
    requestId,
    diagnostics,
  });
}
