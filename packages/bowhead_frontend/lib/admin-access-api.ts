import { responseRequestId } from "./error-presentation";
import { appFetch as fetch } from "@/lib/base-path";

export interface AccessGroup {
  id: string;
  team_id: string;
  workspace_id: string | null;
  name: string;
  description: string;
  status: string;
  member_count: number;
  created_by: string | null;
  created_at: string;
  updated_at: string;
}

export interface AccessGroupMember {
  group_id: string;
  user_id: string;
  role: string;
  email: string | null;
  display_name: string | null;
  created_at: string;
}

export interface ResourceGrant {
  id: string;
  team_id: string;
  resource_kind: string;
  resource_id: string;
  principal_type: string;
  principal_id: string;
  permission: string;
  effect: string;
  created_by: string | null;
  created_at: string;
  updated_at: string;
  expires_at: string | null;
  revoked_at: string | null;
}

export interface PermissionExplainResult {
  allowed: boolean;
  reason: string;
  shadow: boolean;
  grant_id: string | null;
  access_context: {
    user_id?: string;
    team_id?: string;
    workspace_id?: string;
    workspace_role?: string;
    team_role?: string | null;
    group_ids?: string[];
    policy_epoch?: number;
    is_team_admin?: boolean;
  };
}

export class AdminAccessError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    /** Server request ID, for diagnostics only (Copy Diagnostics or collapsed section). */
    public requestId?: string,
    /** Raw backend detail (e.g. "group not found"), only to select localized explanations, never shown directly. */
    public reason?: string,
  ) {
    super(message);
    this.name = "AdminAccessError";
  }
}

export type AdminAccessErrorKey =
  | "error_skill_approval_required"
  | "error_conflict"
  | "error_not_found_member"
  | "error_not_found_user"
  | "error_not_found_group"
  | "error_not_found_space"
  | "error_not_found"
  | "error_invalid"
  | "action_failed";

/**
 * User-facing access-control failure copy (keys under admin.access). Check error code first, then status/known backend detail:
 * E_SKILL_APPROVAL_REQUIRED = skill not approved; 409 = already exists; 404 = member / user / group / space not found;
 * 422 = invalid parameters; otherwise generic copy. Compare detail prefixes longest-first ("group member …" before "group …").
 */
export function adminAccessErrorKey(error: unknown): AdminAccessErrorKey {
  if (!(error instanceof AdminAccessError)) return "action_failed";
  const reason = (error.reason ?? "").toLowerCase();
  if (error.code === "E_SKILL_APPROVAL_REQUIRED") return "error_skill_approval_required";
  if (error.status === 409) return "error_conflict";
  if (error.status === 404) {
    if (reason.startsWith("group member ")) return "error_not_found_member";
    if (reason.startsWith("user ")) return "error_not_found_user";
    if (reason.startsWith("group ")) return "error_not_found_group";
    if (reason.startsWith("workspace ")) return "error_not_found_space";
    return "error_not_found";
  }
  if (error.status === 422) return "error_invalid";
  return "action_failed";
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(path, { ...init, credentials: "include" });
  if (!r.ok) {
    const detail = await r.json().catch(() => ({}));
    const code =
      detail?.detail?.error?.code ??
      detail?.detail?.code ??
      detail?.code ??
      "error";
    // Pages show localized explanations via adminAccessErrorKey; raw API text, status and error codes are never shown directly,
    // only in diagnostics (Copy Diagnostics or collapsed section).
    const message =
      detail?.detail?.error?.message ?? detail?.detail?.message;
    throw new AdminAccessError(
      r.status,
      code,
      typeof message === "string" ? message : "",
      responseRequestId(r),
      typeof detail?.detail === "string" ? detail.detail : undefined,
    );
  }
  if (r.status === 204) return undefined as T;
  return r.json();
}

export function fetchAccessGroups(): Promise<{ items: AccessGroup[] }> {
  return req("/api/v1/groups");
}

export function createAccessGroup(input: {
  name: string;
  description?: string;
  workspace_id?: string | null;
}): Promise<AccessGroup> {
  return req("/api/v1/groups", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export function fetchGroupMembers(groupId: string): Promise<{ items: AccessGroupMember[] }> {
  return req(`/api/v1/groups/${encodeURIComponent(groupId)}/members`);
}

export function addGroupMember(
  groupId: string,
  input: { user_id: string; role?: string },
): Promise<AccessGroupMember> {
  return req(`/api/v1/groups/${encodeURIComponent(groupId)}/members`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export function removeGroupMember(groupId: string, userId: string): Promise<void> {
  return req(`/api/v1/groups/${encodeURIComponent(groupId)}/members/${encodeURIComponent(userId)}`, {
    method: "DELETE",
  });
}

export function fetchResourceGrants(): Promise<{ items: ResourceGrant[] }> {
  return req("/api/v1/resource-grants");
}

export function createResourceGrant(input: {
  resource_kind: string;
  resource_id: string;
  principal_type: string;
  principal_id: string;
  permission: string;
  effect: string;
  expires_at?: string | null;
}): Promise<ResourceGrant> {
  return req("/api/v1/resource-grants", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export function revokeResourceGrant(grantId: string): Promise<void> {
  return req(`/api/v1/resource-grants/${encodeURIComponent(grantId)}`, {
    method: "DELETE",
  });
}

export function explainPermission(input: {
  workspace_id: string;
  resource_kind: string;
  resource_id: string;
  permission: string;
}): Promise<PermissionExplainResult> {
  return req("/api/v1/permissions/explain", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}
