import { apiFetch, readApiError, type ApiError } from "./api-client";

export type TeamSpaceStatus = "active" | "archived";

export interface TeamSpaceRow {
  workspace_id: string;
  name: string;
  description: string | null;
  status: TeamSpaceStatus;
  member_count: number;
  admin_count: number;
  caller_membership: "admin" | "member" | "viewer" | null;
  created_at: string;
  updated_at: string;
  revision: number;
}

export interface SpaceAdminRow {
  user_id: string;
  email: string;
  display_name: string | null;
  role: "admin";
  joined_at: string;
}

export interface TeamMemberCandidate {
  user_id: string;
  email: string;
  display_name: string | null;
}

export class AdminSpacesError extends Error {
  readonly requestId?: ApiError["requestId"];
  readonly diagnostics?: ApiError["diagnostics"];
  readonly validationIssues?: ApiError["validationIssues"];

  constructor(public status: number, public code: string, message: string, source?: ApiError) {
    super(message);
    this.name = "AdminSpacesError";
    this.requestId = source?.requestId;
    this.diagnostics = source?.diagnostics;
    this.validationIssues = source?.validationIssues;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await apiFetch(path, init);
  if (!response.ok) {
    const error = await readApiError(response);
    throw new AdminSpacesError(
      error.status,
      error.code ?? "error",
      error.message,
      error,
    );
  }
  return response.json();
}

export async function fetchTeamSpaces(options: {
  status?: "active" | "archived" | "all";
  q?: string;
} = {}): Promise<TeamSpaceRow[]> {
  const items: TeamSpaceRow[] = [];
  let cursor: string | null = null;
  do {
    const page = await fetchTeamSpacesPage({
      status: options.status ?? "active",
      q: options.q,
      pageSize: 100,
      cursor,
    });
    items.push(...page.items);
    cursor = page.next_cursor;
  } while (cursor);
  return items;
}

export function fetchTeamSpacesPage(options: {
  status: "active" | "archived" | "all";
  q?: string;
  pageSize: number;
  cursor?: string | null;
}): Promise<{ items: TeamSpaceRow[]; next_cursor: string | null }> {
  const params = new URLSearchParams({
    status: options.status,
    page_size: String(options.pageSize),
  });
  if (options.q?.trim()) params.set("q", options.q.trim());
  if (options.cursor) params.set("cursor", options.cursor);
  return request(`/api/v1/admin/spaces?${params.toString()}`);
}

export function createTeamSpace(body: {
  name: string;
  description?: string | null;
  set_as_current?: boolean;
}): Promise<TeamSpaceRow> {
  return request("/api/v1/admin/spaces", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function fetchTeamSpaceAdmins(workspaceId: string): Promise<{
  admins: SpaceAdminRow[];
  team_members: TeamMemberCandidate[];
}> {
  return request(`/api/v1/admin/spaces/${encodeURIComponent(workspaceId)}/admins`);
}

export function grantTeamSpaceAdmin(workspaceId: string, userId: string): Promise<void> {
  return request(`/api/v1/admin/spaces/${encodeURIComponent(workspaceId)}/admins`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user_id: userId }),
  });
}

export function revokeTeamSpaceAdmin(workspaceId: string, userId: string): Promise<void> {
  return request(`/api/v1/admin/spaces/${encodeURIComponent(workspaceId)}/admins/${encodeURIComponent(userId)}`, {
    method: "DELETE",
  });
}

export function archiveTeamSpace(
  workspaceId: string,
  expectedRevision: number,
): Promise<TeamSpaceRow> {
  return request(
    `/api/v1/admin/spaces/${encodeURIComponent(workspaceId)}/archive`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expected_revision: expectedRevision }),
    },
  );
}

export function restoreTeamSpace(
  workspaceId: string,
  expectedRevision: number,
  recoveryAdminUserId?: string,
): Promise<TeamSpaceRow> {
  return request(
    `/api/v1/admin/spaces/${encodeURIComponent(workspaceId)}/restore`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        expected_revision: expectedRevision,
        recovery_admin_user_id: recoveryAdminUserId || null,
      }),
    },
  );
}
