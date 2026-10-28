// content-share-api.ts — typed client for the content-sharing endpoints
// (content-permissions wave 1). Mirrors team-subagents-api.ts: apiFetch +
// readApiError, cookie auth via credentials:include.
//
// Backend: docs/sprints/content-permissions/ — Share API (§5.1) + 选人器 (§5).

import { apiFetch, apiJson, apiVoid } from "./api-client";

export type ShareKind = "report" | "team_subagent" | "team_skill";
export type Audience = "private" | "custom" | "team_wide";
export type PrincipalType = "user" | "group";

export interface ShareGrant {
  id: string;
  principal_type: PrincipalType;
  principal_id: string;
  /** 0708 ⑤: resolved display name (user name / group name); null → fall back to id. */
  principal_name?: string | null;
  permission: string;
  created_by: string | null;
  created_at: string;
}

/** GET /shares/{kind}/{rid} — managers get the full roster; recipients only
 *  see audience + their own grant (info boundary, PRD §5.1). */
export interface ShareState {
  kind: ShareKind;
  resource_id: string;
  label: string;
  audience: Audience;
  owner_id: string | null;
  owner_name?: string | null; // 0708 ⑤: resolved owner display name; null → fall back to owner_id
  can_manage: boolean;
  grants?: ShareGrant[]; // present iff can_manage
  my_grant?: ShareGrant | null; // present iff !can_manage
  pending_requests?: number; // wave 2: open access-requests count (manager view only)
}

export interface DirectoryUser {
  id: string;
  display_name: string;
  email_masked: string;
}
export interface DirectoryGroup {
  id: string;
  name: string;
  member_count: number;
}

/** Per-kind metadata (the design is kind-agnostic; the backend kind drives this).
 *  Only the locale-independent `icon` + the per-kind permission flavor live here;
 *  the user-visible typeLabel / verb / perm strings are resolved via `t()` at the
 *  call site (ShareDialog) so they switch with the active locale. verb / perm
 *  wording per PRD §4.4. */
export const KIND_META: Record<
  ShareKind,
  { icon: "doc" | "bot" | "spark"; perm: "view" | "use" }
> = {
  report: { icon: "doc", perm: "view" },
  team_subagent: { icon: "bot", perm: "use" },
  team_skill: { icon: "spark", perm: "use" },
};

function base(kind: ShareKind, rid: string): string {
  return `/api/v1/shares/${kind}/${encodeURIComponent(rid)}`;
}

export function getShares(kind: ShareKind, rid: string): Promise<ShareState> {
  return apiJson<ShareState>(base(kind, rid));
}

export function putAudience(
  kind: ShareKind,
  rid: string,
  audience: Audience,
): Promise<{ kind: ShareKind; resource_id: string; audience: Audience; revoked: number }> {
  return apiJson(`${base(kind, rid)}/audience`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ audience }),
  });
}

export function addShareGrant(
  kind: ShareKind,
  rid: string,
  principal: { principal_type: PrincipalType; principal_id: string },
): Promise<ShareGrant> {
  return apiJson<ShareGrant>(`${base(kind, rid)}/grants`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(principal),
  });
}

export function revokeShareGrant(
  kind: ShareKind,
  rid: string,
  grantId: string,
): Promise<void> {
  return apiVoid(`${base(kind, rid)}/grants/${encodeURIComponent(grantId)}`, {
    method: "DELETE",
  });
}

export async function fetchTeamDirectory(
  q?: string,
  cursor?: number,
): Promise<{ items: DirectoryUser[]; next_cursor: number | null }> {
  // ⑥ (bug 0708): backend paginates (limit 50 + next_cursor); pass cursor so the
  // dropdown can scroll-load page 2+. Omitting it always returned page 1 only.
  const params = new URLSearchParams();
  if (q) params.set("q", q);
  if (typeof cursor === "number" && cursor > 0) params.set("cursor", String(cursor));
  const qs = params.toString() ? `?${params.toString()}` : "";
  const r = await apiFetch(`/api/v1/team/directory${qs}`);
  if (!r.ok) return { items: [], next_cursor: null };
  return (await r.json()) as { items: DirectoryUser[]; next_cursor: number | null };
}

export async function fetchTeamGroups(): Promise<{ items: DirectoryGroup[] }> {
  const r = await apiFetch(`/api/v1/team/groups-directory`);
  if (!r.ok) return { items: [] };
  return (await r.json()) as { items: DirectoryGroup[] };
}
