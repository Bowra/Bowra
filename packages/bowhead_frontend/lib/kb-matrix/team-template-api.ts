/**
 * kb-matrix-sharing — team-template library API client
 * (gateway routes/kb_team_templates.py: publish / discover / copy / revoke).
 *
 * All use appFetch (URI-prefix routing, M4 rebase pitfall) + spaceHeader + matrixJsonOrThrow
 * (typed MatrixApiError.code branches: not_publishable / empty_template /
 * schema_too_large / doc_type_name_taken [with suggestion] / copy_into_source_space /
 * invalid_template_schema / workspace_admin_or_creator_required / revoke_not_allowed).
 * Persisted copy but failed readback = team_template_copy_refresh_required (refresh to confirm before retrying).
 * Cross-team/nonexistent/revoked = uniform 404 (team-keyed anti-enumeration, no cross_team code).
 */

import { appFetch as fetch } from "@/lib/base-path";
import { type KbDocTypeDTO } from "@/lib/kb-knowledge-tables-api";
import { spaceHeader } from "@/lib/space-storage";

import { MatrixApiError, matrixJsonOrThrow as orThrow } from "./api-error";

export { MatrixApiError };

/** Discovery entry (server _entry_to_dict projection): **no data**, schema preview only. */
export interface TeamTemplateDTO {
  entry_id: string;
  /** Space doc_type ↔ library-entry matching anchor (design review P1; determine published/drift and entry_id). */
  source_doc_type_id: string;
  name: string;
  description: string;
  field_count: number;
  field_preview: Array<{ name: string; type: string }>;
  source_workspace_id: string;
  source_workspace_name: string | null;
  published_by: string;
  published_by_name: string | null;
  published_at: string;
  updated_at: string;
  /** Present only when list includes workspace_id: entry source space == current space. */
  is_own_space?: boolean;
  /** Present only when list includes workspace_id: current space already has a copy of this entry. */
  already_copied?: boolean;
}

function headers(json = false): Record<string, string> {
  const h: Record<string, string> = typeof window === "undefined" ? {} : spaceHeader();
  if (json) h["content-type"] = "application/json";
  return h;
}

/** Publish kind=template doc_type to team library (idempotent per source_doc_type_id). */
export async function publishTemplateToTeam(workspaceId: string, docTypeId: string): Promise<TeamTemplateDTO> {
  const r = await fetch(`/api/v1/kb/team-templates`, {
    method: "POST",
    headers: headers(true),
    body: JSON.stringify({ workspace_id: workspaceId, doc_type_id: docTypeId }),
  });
  return orThrow(r, "publishTemplateToTeam");
}

/** List team's published templates. workspaceId adds convenient is_own_space / already_copied flags. */
export async function listTeamTemplates(workspaceId?: string): Promise<TeamTemplateDTO[]> {
  const qs = workspaceId ? `?workspace_id=${encodeURIComponent(workspaceId)}` : "";
  const r = await fetch(`/api/v1/kb/team-templates${qs}`, { headers: headers() });
  const data = await orThrow<{ items: TeamTemplateDTO[] }>(r, "listTeamTemplates");
  return data.items;
}

/** Copy library entry to target space (materialize kind=template + lineage); name override renames on collision. */
export async function copyTeamTemplate(workspaceId: string, entryId: string, name?: string): Promise<KbDocTypeDTO> {
  const r = await fetch(`/api/v1/kb/team-templates/${encodeURIComponent(entryId)}/copy`, {
    method: "POST",
    headers: headers(true),
    body: JSON.stringify(name ? { workspace_id: workspaceId, name } : { workspace_id: workspaceId }),
  });
  return orThrow(r, "copyTeamTemplate");
}

/** Revoke a library entry's publication. */
export async function revokeTeamTemplate(entryId: string): Promise<void> {
  const r = await fetch(`/api/v1/kb/team-templates/${encodeURIComponent(entryId)}`, {
    method: "DELETE",
    headers: headers(),
  });
  if (!r.ok) await orThrow(r, "revokeTeamTemplate");
}

// ─── Pure functions (publication-state derivation, enforced by UT) ───────────────────────────────

export type PublishState = "none" | "published" | "drift";

/** My templates card publication state: match source_doc_type_id → none/published/drift.
 * drift = local template updated_at later than library entry updated_at (fields changed after publication). */
export function publishStateOf(dt: KbDocTypeDTO, entry: TeamTemplateDTO | undefined): PublishState {
  if (!entry) return "none";
  if (dt.updated_at && entry.updated_at && dt.updated_at > entry.updated_at) return "drift";
  return "published";
}

/** doc_type_id → its team-library entry (match source_doc_type_id, rename-proof). */
export function indexEntriesBySource(entries: TeamTemplateDTO[]): Map<string, TeamTemplateDTO> {
  return new Map(entries.map((e) => [e.source_doc_type_id, e]));
}
