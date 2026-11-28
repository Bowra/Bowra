/**
 * Knowledge bases (organization phase 1, behind FEATURE_KNOWLEDGE_BASES): typed client for
 * `/api/v1/kb/knowledge-bases` (gateway `routes/kb_knowledge_bases.py`, technical design §3 and §3.1).
 *
 * Every route answers 404 `knowledge_base_not_found` while the switch is off, so callers render
 * these pages only when `/me` reports `capabilities.knowledge_bases`. Errors are `ApiError`s; map
 * them with `kbFileError` (`lib/kb-file-error.ts`), which knows every finite code of this API.
 */
import { appFetch as fetch } from "@/lib/base-path";
import { readApiError } from "@/lib/api-client";
import { createSharedResource } from "@/lib/shared-resource";
import { spaceHeader } from "@/lib/space-storage";
import { TEAM_CHANGED_EVENT } from "@/lib/team-storage";

import type { KbFileDTO, KbFileListResponse } from "./kb-api";

/** Where a knowledge base is used: My (only me, every Space), one Space, or the whole Team. */
export type KnowledgeBaseHome = "personal" | "space" | "team";
/** Who outside the member list can use it: My is `owner`; Space `members` | `space`; Team `members` | `team`. */
export type KnowledgeBaseAudience = "owner" | "members" | "space" | "team";
/** Access levels; owners and automatic managers are reported as `manage`. */
export type KnowledgeBaseLevel = "use" | "edit" | "manage";

export const KNOWLEDGE_BASE_HOMES: readonly KnowledgeBaseHome[] = ["personal", "space", "team"];
export const KNOWLEDGE_BASE_LEVELS: readonly KnowledgeBaseLevel[] = ["use", "edit", "manage"];

export interface KnowledgeBaseOwner {
  user_id: string;
  /** Display name; null when it could not be resolved (render a neutral fallback, never the ID). */
  name: string | null;
}

export interface KnowledgeBaseDTO {
  id: string;
  /** Default knowledge bases have no stored name: render it from the home (`knowledgeBaseName`). */
  is_default: boolean;
  home: KnowledgeBaseHome;
  /** User ID (My), Space ID (Space) or Team ID (Team). */
  home_id: string;
  name: string | null;
  description: string | null;
  audience: KnowledgeBaseAudience;
  /** The default level of the "all Space members" audience; "use" otherwise. */
  audience_level: "use" | "edit";
  /** The caller's level. */
  level: KnowledgeBaseLevel;
  owner: KnowledgeBaseOwner | null;
  can: {
    add_files: boolean;
    manage: boolean;
    transfer: boolean;
    delete: boolean;
  };
  file_count: number;
  failed_count: number;
  /** The later of its own and its newest file's update time. */
  updated_at: string | null;
  revision: number;
}

export interface KnowledgeBaseDirectory {
  /** The Space the Space section belongs to; null without a current Space. */
  space_id: string | null;
  sections: Record<KnowledgeBaseHome, KnowledgeBaseDTO[]>;
  /** Homes the caller may create in (PRD §3: My everyone; Space admins and members; Team owner and admins). */
  can_create: Record<KnowledgeBaseHome, boolean>;
}

export type KnowledgeBasePrincipalType = "user" | "group" | "space";

export type KnowledgeBaseEntryState =
  | "active"
  | "not_in_team"
  | "not_in_space"
  | "group_removed"
  | "not_applicable"
  | "space_removed";

export interface KnowledgeBaseMemberEntry {
  principal_type: KnowledgeBasePrincipalType;
  principal_id: string;
  /** Person, group or Space name; null when it no longer resolves. */
  label: string | null;
  level: KnowledgeBaseLevel;
  state: KnowledgeBaseEntryState;
  added_by: string;
  created_at: string;
}

export interface KnowledgeBaseMembers {
  knowledge_base_id: string;
  audience: KnowledgeBaseAudience;
  audience_level: "use" | "edit";
  owner: KnowledgeBaseOwner | null;
  /** Space admins (Space knowledge bases; C2 adds Team admins on a Space default) and Team admins (Team ones). */
  automatic_managers: Array<KnowledgeBaseOwner & { reason: "space_admin" | "team_admin" }>;
  entries: KnowledgeBaseMemberEntry[];
  /** Each person once, at their highest level. */
  summary: { use: number; edit: number; manage: number; people: number };
  can_manage: boolean;
}

export interface KnowledgeBaseMemberChange {
  principal_type: KnowledgeBasePrincipalType;
  principal_id: string;
  /** null removes the entry. */
  level: KnowledgeBaseLevel | null;
}

export interface KnowledgeBaseCreate {
  name: string;
  description?: string | null;
  home: KnowledgeBaseHome;
  audience: KnowledgeBaseAudience;
  audience_level?: "use" | "edit";
}

export interface KnowledgeBasePatch {
  revision: number;
  name?: string;
  description?: string | null;
  audience?: KnowledgeBaseAudience;
  audience_level?: "use" | "edit";
}

export interface KnowledgeBaseMoveItem {
  file_id: string;
  ok: boolean;
  /** Already in the target knowledge base. */
  unchanged?: boolean;
  /** A finite API code (map with `kbFileError`). */
  error_code?: string;
}

export interface KnowledgeBaseMoveResult {
  dry_run: boolean;
  target_knowledge_base_id: string;
  results: KnowledgeBaseMoveItem[];
  succeeded: number;
  failed: number;
  unchanged: number;
  /** Dry run only: files that would move, and how many people gain or lose access. */
  movable?: number;
  gains?: number;
  losses?: number;
}

/** File-table parameters of one knowledge base (the same filters, sort and paging as `/kb/files`, without a scope). */
export interface KnowledgeBaseFileListParams {
  cursor?: string;
  offset?: number;
  limit?: number;
  file_ids?: readonly string[];
  status?: string;
  mime?: string;
  doc_type_id?: string;
  uploaded_after?: string;
  uploaded_before?: string;
  q?: string;
  order_by?: "uploaded_at" | "filename" | "size_bytes";
  order_dir?: "asc" | "desc";
}

const BASE = "/api/v1/kb/knowledge-bases";

function path(id: string, rest = ""): string {
  return `${BASE}/${encodeURIComponent(id)}${rest}`;
}

async function request<T>(url: string, init: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = { ...spaceHeader() };
  if (init.body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(url, { ...init, headers: { ...headers, ...(init.headers as Record<string, string> | undefined) } });
  if (!response.ok) throw await readApiError(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export function listKnowledgeBases(): Promise<KnowledgeBaseDirectory> {
  return request<KnowledgeBaseDirectory>(BASE);
}

export function getKnowledgeBase(id: string): Promise<KnowledgeBaseDTO> {
  return request<KnowledgeBaseDTO>(path(id));
}

export function createKnowledgeBase(body: KnowledgeBaseCreate): Promise<KnowledgeBaseDTO> {
  return request<KnowledgeBaseDTO>(BASE, { method: "POST", body: JSON.stringify(body) });
}

export function updateKnowledgeBase(id: string, body: KnowledgeBasePatch): Promise<KnowledgeBaseDTO> {
  return request<KnowledgeBaseDTO>(path(id), { method: "PATCH", body: JSON.stringify(body) });
}

/** Delete a named knowledge base; its files leave knowledge and their originals stay in Files (Q5). */
export function deleteKnowledgeBase(
  id: string,
  confirm: { file_count: number; revision?: number },
): Promise<{ deleted: boolean; knowledge_base_id: string; files_removed: number }> {
  const qs = new URLSearchParams({ file_count: String(confirm.file_count) });
  if (confirm.revision != null) qs.set("revision", String(confirm.revision));
  return request(path(id, `?${qs.toString()}`), { method: "DELETE" });
}

export function getKnowledgeBaseMembers(id: string): Promise<KnowledgeBaseMembers> {
  return request<KnowledgeBaseMembers>(path(id, "/members"));
}

export function putKnowledgeBaseMembers(id: string, changes: KnowledgeBaseMemberChange[]): Promise<KnowledgeBaseMembers> {
  return request<KnowledgeBaseMembers>(path(id, "/members"), { method: "PUT", body: JSON.stringify({ changes }) });
}

export function transferKnowledgeBase(id: string, body: { user_id: string; revision?: number }): Promise<KnowledgeBaseDTO> {
  return request<KnowledgeBaseDTO>(path(id, "/transfer"), { method: "POST", body: JSON.stringify(body) });
}

export function listKnowledgeBaseFiles(id: string, opts: KnowledgeBaseFileListParams = {}): Promise<KbFileListResponse> {
  const qs = new URLSearchParams();
  if (opts.cursor) qs.set("cursor", opts.cursor);
  if (opts.offset != null) qs.set("offset", String(opts.offset));
  if (opts.limit != null) qs.set("limit", String(opts.limit));
  opts.file_ids?.forEach((fileId) => qs.append("file_ids", fileId));
  if (opts.status) qs.set("status", opts.status);
  if (opts.mime) qs.set("mime", opts.mime);
  if (opts.doc_type_id) qs.set("doc_type_id", opts.doc_type_id);
  if (opts.uploaded_after) qs.set("uploaded_after", opts.uploaded_after);
  if (opts.uploaded_before) qs.set("uploaded_before", opts.uploaded_before);
  if (opts.q) qs.set("q", opts.q);
  if (opts.order_by) qs.set("order_by", opts.order_by);
  if (opts.order_dir) qs.set("order_dir", opts.order_dir);
  const query = qs.toString();
  return request<KbFileListResponse>(path(id, `/files${query ? `?${query}` : ""}`));
}

/** Move files to another knowledge base; `dry_run` returns the access change (gains and losses) without moving. */
export function moveKnowledgeBaseFiles(body: {
  file_ids: string[];
  target_knowledge_base_id: string;
  dry_run?: boolean;
  reason?: string;
}): Promise<KnowledgeBaseMoveResult> {
  return request<KnowledgeBaseMoveResult>(`${BASE}/files/move`, { method: "POST", body: JSON.stringify(body) });
}

// ─── shared directory read ─────────────────────────────────────────────

/** Broadcast after any knowledge base write (create, rename, delete, members, moves, uploads) to refresh readers. */
export const KNOWLEDGE_BASES_CHANGED_EVENT = "bowhead:knowledge-bases-changed";

export function notifyKnowledgeBasesChanged(): void {
  if (typeof window !== "undefined") window.dispatchEvent(new Event(KNOWLEDGE_BASES_CHANGED_EVENT));
}

/**
 * One directory read per current Space, shared by the directory, the upload and move pickers and the file
 * details. The key is the Space ID ("" without one); the request carries the active Space header, which is
 * the same Space.
 */
export const knowledgeBaseDirectoryResource = createSharedResource<KnowledgeBaseDirectory>({
  name: "knowledge-base-directory",
  fetcher: () => listKnowledgeBases(),
  ttlMs: 15_000,
  invalidateOnEvents: [KNOWLEDGE_BASES_CHANGED_EVENT, TEAM_CHANGED_EVENT],
  contextEvents: [TEAM_CHANGED_EVENT],
});

/** Every knowledge base of a directory, in section order (My, Space, Team). */
export function directoryKnowledgeBases(directory: KnowledgeBaseDirectory | null | undefined): KnowledgeBaseDTO[] {
  if (!directory) return [];
  return KNOWLEDGE_BASE_HOMES.flatMap((home) => directory.sections[home] ?? []);
}

/**
 * The knowledge base a file is in. A file response carries `knowledge_base_id` while the switch is on; a
 * missing value means the default of the file's scope (technical design §1.2 read safety rule).
 */
export function knowledgeBaseOfFile(
  file: Pick<KbFileDTO, "scope" | "scope_id"> & { knowledge_base_id?: string | null },
  directory: KnowledgeBaseDirectory | null | undefined,
): KnowledgeBaseDTO | null {
  const all = directoryKnowledgeBases(directory);
  if (file.knowledge_base_id) {
    const named = all.find((kb) => kb.id === file.knowledge_base_id);
    if (named) return named;
  }
  const home: KnowledgeBaseHome = (file.scope as string) === "workspace" ? "space" : file.scope;
  return all.find((kb) => kb.is_default && kb.home === home && kb.home_id === file.scope_id) ?? null;
}
