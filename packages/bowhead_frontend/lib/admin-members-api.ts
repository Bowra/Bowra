/**
 * Sprint β G5 — admin members FE API.
 *
 * Backend: GET /api/v1/admin/members
 *          POST /api/v1/admin/members/{user_id}/role
 * Gate: owner | team_admin | audit_admin
 */

import { apiFetch, readApiError, type ApiError } from "./api-client";


export type TeamRole =
  | "owner"
  | "team_admin"

  | "audit_admin"
  | "member";


export interface AdminMemberRow {
  user_id: string;
  email: string;
  display_name: string | null;
  tier: string;
  is_owner: boolean;
  team_role: TeamRole;
  promoted_at: string | null;
  created_at: string;
  last_active_at: string | null;
}

export interface TeamInviteRow {
  id: string;
  team_id: string;
  team_role: "member" | string;
  invited_email: string;
  status: "active" | "revoked" | "consumed" | string;
  invited_by: string | null;
  created_at: string;
  expires_at: string;
  consumed_at: string | null;
  revoked_at: string | null;
  assignment_mode?: "spaces" | "team_only" | "team_only_legacy";
  assignment_revision?: number;
  initial_workspace_grants?: InitialWorkspaceGrant[];
}

export interface InitialWorkspaceGrant {
  workspace_id: string;
  role: "admin" | "member" | "viewer";
}

export interface TeamInviteResp {
  ok: boolean;
  method:
    | "email_invite"
    | "created"
    | "grant_merged"
    | "already_present"
    | "already_member"
    | "already_invited";
  invite_id?: string;
  invite_url?: string;
  email_sent?: boolean;
  user_id?: string;
  assignment_mode?: "spaces" | "team_only" | "team_only_legacy";
  assignment_revision?: number;
  initial_workspace_grants?: InitialWorkspaceGrant[];
}

export interface WorkspaceGrantRow {
  workspace_id: string;
  name: string;
  description: string | null;
  status: "active" | string;
  role: "admin" | "member" | "viewer" | null;
  joined_at: string | null;
}

export interface WorkspaceGrantResp {
  user_id: string;
  items: WorkspaceGrantRow[];
}

export interface AdminMembersListResp {
  items: AdminMemberRow[];
  owner_id: string | null;
  your_role: TeamRole;
  total?: number;
  page?: number | null;
  size?: number | null;
  non_owner_team_admin_count?: number;
}

export interface TeamInvitesPage {
  invites: TeamInviteRow[];
  total: number;
  page: number;
  size: number;
}


export interface RoleChangeResp {
  ok: boolean;
  method: "changed" | "noop";
  old_role: TeamRole | null;
  new_role: TeamRole;
}

export interface OwnerTransferResp {
  ok: boolean;
  owner_user_id: string;
}


export interface LocalMemberCreateResp {
  id: string;
  email: string;
  display_name: string;
  method: "created" | "member_added" | "membership_restored" | "already_member";
  recovery_path: string | null;
  recovery_expires_at: string | null;
}


export class AdminMembersError extends Error {
  readonly requestId?: ApiError["requestId"];
  readonly diagnostics?: ApiError["diagnostics"];
  readonly validationIssues?: ApiError["validationIssues"];

  constructor(public status: number, public code: string, message: string, source?: ApiError) {
    super(message);
    this.name = "AdminMembersError";
    this.requestId = source?.requestId;
    this.diagnostics = source?.diagnostics;
    this.validationIssues = source?.validationIssues;
  }
}


async function _req<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const r = await apiFetch(path, init);
  if (!r.ok) {
    const error = await readApiError(r);
    throw new AdminMembersError(
      error.status,
      error.code ?? "error",
      error.message,
      error,
    );
  }
  return r.json();
}


export function fetchAdminMembers(opts?: {
  page: number;
  size: number;
  q?: string;
  role?: TeamRole | "";
}): Promise<AdminMembersListResp> {
  if (!opts) return _req("/api/v1/admin/members");
  const qs = new URLSearchParams({ page: String(opts.page), size: String(opts.size) });
  if (opts.q) qs.set("q", opts.q);
  if (opts.role) qs.set("role", opts.role);
  return _req(`/api/v1/admin/members?${qs}`);
}


export function changeTeamRole(
  userId: string,
  newRole: TeamRole,
  reason?: string,
): Promise<RoleChangeResp> {
  return _req(`/api/v1/admin/members/${encodeURIComponent(userId)}/role`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ new_role: newRole, reason }),
  });
}

export function transferTeamOwnership(userId: string): Promise<OwnerTransferResp> {
  return _req("/api/v1/admin/members/owner-transfer", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ target_user_id: userId }),
  });
}


export function inviteTeamMember(
  email: string,
  assignment?: {
    assignment_mode: "spaces" | "team_only";
    initial_workspace_grants: InitialWorkspaceGrant[];
  },
): Promise<TeamInviteResp> {
  return _req("/api/v1/admin/members/invite", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, ...assignment }),
  });
}

export function createLocalMember(body: {
  email: string;
  display_name: string;
}): Promise<LocalMemberCreateResp> {
  return _req("/api/v1/admin/members", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function fetchTeamInvitesPage(opts: {
  page: number;
  size: number;
  q?: string;
}): Promise<TeamInvitesPage> {
  const qs = new URLSearchParams({ page: String(opts.page), size: String(opts.size) });
  if (opts.q) qs.set("q", opts.q);
  return _req(`/api/v1/admin/members/invites?${qs}`);
}


export function resendTeamInvite(inviteId: string): Promise<TeamInviteResp> {
  return _req(`/api/v1/admin/members/invites/${encodeURIComponent(inviteId)}/resend`, {
    method: "POST",
  });
}


export function revokeTeamInvite(inviteId: string): Promise<{ ok: boolean }> {
  return _req(`/api/v1/admin/members/invites/${encodeURIComponent(inviteId)}`, {
    method: "DELETE",
  });
}


export function deactivateTeamMember(userId: string): Promise<{ ok: boolean }> {
  return _req(`/api/v1/admin/team/members/${encodeURIComponent(userId)}/deactivate`, {
    method: "POST",
  });
}

export function createCredentialRecovery(
  userId: string,
): Promise<{
  ok: boolean;
  delivery: "manual";
  recovery_url: string;
  expires_at: string;
}> {
  return _req(`/api/v1/admin/members/${encodeURIComponent(userId)}/credential-recovery`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
  });
}

export function revokeCredentialRecovery(
  userId: string,
): Promise<{ ok: boolean; revoked_count: number }> {
  return _req(`/api/v1/admin/members/${encodeURIComponent(userId)}/credential-recovery/revoke`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });
}


export function fetchMemberWorkspaceGrants(userId: string): Promise<WorkspaceGrantResp> {
  return _req(`/api/v1/admin/members/${encodeURIComponent(userId)}/workspace-grants`);
}

export function upsertMemberWorkspaceGrant(
  userId: string,
  workspaceId: string,
  role: "admin" | "member" | "viewer",
): Promise<{ ok: boolean }> {
  return _req(`/api/v1/admin/members/${encodeURIComponent(userId)}/workspace-grants`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ workspace_id: workspaceId, role }),
  });
}


export function revokeMemberWorkspaceGrant(
  userId: string,
  workspaceId: string,
): Promise<{ ok: boolean }> {
  return _req(
    `/api/v1/admin/members/${encodeURIComponent(userId)}/workspace-grants/${encodeURIComponent(workspaceId)}`,
    { method: "DELETE" },
  );
}
