/**
 * Sprint β G10 — change-password + active sessions FE API.
 *
 * Backend: POST /api/v1/auth/change-password
 *          GET  /api/v1/auth/sessions/active
 *          DELETE /api/v1/auth/sessions/{id}
 */


import { apiFetch, readApiError } from "./api-client";
import {
  clearInvalidSessionAndRedirect,
  refreshAccessToken,
} from "./auth-api";
import { appFetch } from "./base-path";

export interface ChangePasswordResp {
  ok: boolean;
  access_token: string;
  other_devices_kicked: number;
}


export interface SessionRow {
  session_id: string;
  expires_at: string;
  created_at: string;
  last_used_at: string | null;
  user_agent: string | null;
  ip_masked: string;
  is_current: boolean;
}


export interface SessionsListResp {
  items: SessionRow[];
  current_identified: boolean;
}


export class SecurityError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    /** Preserve ApiError's server request ID for diagnostics only (Copy Diagnostics or the collapsible area). */
    public requestId?: string,
  ) {
    super(message);
    this.name = "SecurityError";
  }
}


async function _send<T>(
  path: string,
  method: "POST" | "DELETE" | "GET",
  body?: unknown,
): Promise<T> {
  const init: RequestInit = {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
  };
  if (body) init.body = JSON.stringify(body);
  const r = await apiFetch(path, init);
  if (!r.ok) {
    const error = await readApiError(r);
    throw new SecurityError(
      error.status,
      error.code ?? "error",
      error.message,
      error.requestId,
    );
  }
  return r.json();
}


export async function changePassword(payload: {
  old_password: string;
  new_password: string;
}): Promise<ChangePasswordResp> {
  let response = await _sendChangePassword(payload);
  if (response.status !== 401) return _readChangePasswordResponse(response);

  const firstError = await _readSecurityError(response);
  if (firstError.code === "wrong_old_password") throw firstError;

  try {
    await refreshAccessToken();
  } catch {
    await clearInvalidSessionAndRedirect();
    throw firstError;
  }

  response = await _sendChangePassword(payload);
  if (response.status === 401) {
    const retryError = await _readSecurityError(response);
    if (retryError.code !== "wrong_old_password") {
      await clearInvalidSessionAndRedirect();
    }
    throw retryError;
  }
  return _readChangePasswordResponse(response);
}

function _sendChangePassword(payload: {
  old_password: string;
  new_password: string;
}): Promise<Response> {
  return appFetch("/api/v1/auth/change-password", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    credentials: "include",
  });
}

async function _readChangePasswordResponse(
  response: Response,
): Promise<ChangePasswordResp> {
  if (!response.ok) {
    throw await _readSecurityError(response);
  }
  return response.json();
}

async function _readSecurityError(
  response: Response,
): Promise<SecurityError> {
  const error = await readApiError(response);
  return new SecurityError(
    error.status,
    error.code ?? "error",
    error.message,
    error.requestId,
  );
}


export function fetchActiveSessions(): Promise<SessionsListResp> {
  return _send("/api/v1/auth/sessions/active", "GET");
}


export function revokeSession(sessionId: string): Promise<{ ok: boolean }> {
  return _send(
    `/api/v1/auth/sessions/${encodeURIComponent(sessionId)}`,
    "DELETE",
  );
}
