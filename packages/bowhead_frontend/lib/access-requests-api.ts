// access-requests-api.ts — typed client for the access-request + approval flow
// (content-permissions wave 2). Mirrors content-share-api.ts (apiJson, cookie auth).
//
// Backend: docs/sprints/access-requests/ — uniform request intake for reports, team skills
// and team agents, the approver list + decide (with decision options), and the requester's
// status / withdraw / scope=mine endpoints.

import { ApiError, apiJson } from "./api-client";
import { notifySettingsNavCountsChanged } from "./settings-nav-signal";

/** Kinds accepted by the intake, status and withdraw endpoints. */
export type AccessRequestKind = "report" | "team_skill" | "team_subagent";

export interface AccessRequestItem {
  id: string;
  kind: string;
  resource_id: string;
  label: string;
  requester_id: string;
  requester_name: string;
  requester_email: string | null;
  message: string | null;
  status: string;
  prior_denied: number;
  created_at: string;
  decided_by: string | null;
  decided_at: string | null;
  decision_note?: string | null;
  granted_permission?: string | null;
  grant_expires_at?: string | null;
  expires_at?: string | null;
  withdrawn_at?: string | null;
}

/** Requester-facing state. Deleted, failed and unanswered requests all read as `expired`. */
export type RequesterState =
  | "none"
  | "pending"
  | "approved"
  | "approved_pending"
  | "denied"
  | "withdrawn"
  | "expired";

/** The requester's own view of one request (scope=mine, /status). Never names the approver. */
export interface RequesterRequestView {
  request_id: string;
  kind: string;
  resource_id: string;
  state: Exclude<RequesterState, "none">;
  message: string | null;
  note: string | null;
  created_at: string;
  expires_at: string | null;
  decided_at: string | null;
  granted_permission: string | null;
  grant_expires_at: string | null;
  retry_after: number | null;
  retry_at: string | null;
}

export interface AccessRequestStatus {
  state: RequesterState;
  request: RequesterRequestView | null;
  retry_after: number | null;
}

export interface DecideOptions {
  note?: string;
  /** Approve only; never for agents. */
  grantExpiresInDays?: 30;
  /** Deny only: stop accepting this person's requests for this item for 30 days. */
  stopRequests?: boolean;
}

/** POST /access-requests — uniform 202 (anti-enumeration): the same response, with a
 *  `request_id`, whether or not the resource exists. May answer 409 (cooldown) or 429 (rate
 *  limited) based only on the requester's own history. */
export function requestAccess(
  kind: AccessRequestKind,
  resourceId: string,
  message?: string,
): Promise<{ status: string; request_id?: string }> {
  return apiJson("/api/v1/access-requests", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ kind, resource_id: resourceId, message: message || undefined }),
  });
}

/** GET /access-requests — requests this user can decide (owner ∪ team-admin). */
export function listAccessRequests(
  scope: "pending" | "history" = "pending",
): Promise<{ items: AccessRequestItem[] }> {
  return apiJson(`/api/v1/access-requests?scope=${scope}`);
}

/** GET /access-requests?scope=mine — the caller's own requests. */
export function listMyAccessRequests(): Promise<{ items: RequesterRequestView[] }> {
  return apiJson("/api/v1/access-requests?scope=mine");
}

/** GET /access-requests/status — the caller's latest request for one item. */
export function getAccessRequestStatus(
  kind: AccessRequestKind,
  resourceId: string,
): Promise<AccessRequestStatus> {
  const query = new URLSearchParams({ kind, resource_id: resourceId });
  return apiJson(`/api/v1/access-requests/status?${query.toString()}`);
}

/** POST /access-requests/{id}/withdraw. */
export function withdrawAccessRequest(id: string): Promise<{ status: string }> {
  return apiJson(`/api/v1/access-requests/${encodeURIComponent(id)}/withdraw`, { method: "POST" });
}

/** POST /access-requests/{id}/decide. Without `options` the body is just `{action}`. */
export function decideAccessRequest(
  id: string,
  action: "approve" | "deny",
  options?: DecideOptions,
): Promise<{ status: string }> {
  const body: Record<string, unknown> = { action };
  if (options) {
    const note = options.note?.trim();
    if (note) body.note = note;
    if (action === "approve" && options.grantExpiresInDays) body.grant_expires_in_days = options.grantExpiresInDays;
    if (action === "deny" && options.stopRequests) body.stop_requests = true;
  }
  return Promise.resolve(apiJson<{ status: string }>(`/api/v1/access-requests/${encodeURIComponent(id)}/decide`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  })).then((result) => {
    // The sidebar's pending pill reads a shared, TTL-cached count.
    notifySettingsNavCountsChanged();
    return result;
  });
}

function detailRecord(error: ApiError): Record<string, unknown> {
  const body = error.body;
  if (!body || typeof body !== "object") return {};
  const detail = (body as Record<string, unknown>).detail;
  if (detail && typeof detail === "object" && !Array.isArray(detail)) return detail as Record<string, unknown>;
  return body as Record<string, unknown>;
}

/** The coded reason of an access-request failure, from the error code or the raw detail. */
export function accessRequestErrorCode(error: unknown): string | null {
  if (!(error instanceof ApiError)) return null;
  if (error.code) return error.code;
  const code = detailRecord(error).code;
  return typeof code === "string" ? code : null;
}

/** Seconds until the requester may try again (409 cooldown / 429 rate limit), if given. */
export function accessRequestRetryAfter(error: unknown): number | null {
  if (!(error instanceof ApiError)) return null;
  const value = detailRecord(error).retry_after;
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? value : null;
}
