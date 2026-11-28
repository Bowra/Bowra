/**
 * Knowledge base grant report (organization phase 1, slice 6, behind FEATURE_KNOWLEDGE_BASES): typed client for
 * `GET /api/v1/resource-grants/knowledge-base-report` (gateway `kb_grant_report.py`).
 *
 * Read-only. Every `kb_scope` and `kb_file` grant of the Team with who it applies to, its target, what it
 * restricts today and a suggested mapping to knowledge base members. The route answers 404 while the switch is
 * off, so callers render the report only when `/me` reports `capabilities.knowledge_bases`.
 */
import { appFetch as fetch } from "@/lib/base-path";
import { readApiError } from "@/lib/api-client";

import type { KnowledgeBaseAudience, KnowledgeBaseHome } from "./knowledge-bases-api";

export interface GrantReportKnowledgeBase {
  id: string;
  /** Defaults have no name: render it from the home (`knowledgeBaseName`, with `home_name` for Spaces). */
  is_default: boolean;
  home: KnowledgeBaseHome;
  home_id: string;
  name: string | null;
  /** The Space's name for Space homes. */
  home_name: string | null;
  audience: KnowledgeBaseAudience;
  audience_level: "use" | "edit";
}

export type GrantReportPrincipalState = "active" | "not_in_team" | "group_removed" | "space_archived" | "not_found";

export interface GrantReportPrincipal {
  /** As stored: `user`, `group`, `workspace` (a Space's members) or `team` (everyone in the Team). */
  type: string;
  id: string;
  /** Person, group or Space name; null when it does not resolve. */
  label: string | null;
  state: GrantReportPrincipalState;
  /** Groups only: a Space-bound group's Space and the active member count. */
  space_name: string | null;
  member_count: number | null;
}

export type GrantReportTargetState = "active" | "hidden" | "deleted" | "not_found" | "space_archived" | "unrecognized" | "unavailable";

export interface GrantReportTarget {
  kind: "kb_scope" | "kb_file";
  home: KnowledgeBaseHome | null;
  space_name: string | null;
  /** Files only, and only when you can open the file. */
  file_name: string | null;
  state: GrantReportTargetState;
  /** The knowledge base the file is in, when you can open it. */
  knowledge_base: GrantReportKnowledgeBase | null;
}

export type GrantReportAction = "read" | "write";

export interface GrantReportRestriction {
  /** `allow_list`: only principals with an allow grant get through; `actions` is what this one keeps. `deny`: it loses `actions`. */
  kind: "allow_list" | "deny" | "none";
  actions: GrantReportAction[];
  reason: "expired" | "permission_not_checked" | null;
}

export type GrantReportMappingReason = "expired" | "permission_not_checked" | "target_unavailable" | "principal_unavailable" | "personal";

export interface GrantReportMapping {
  /** A suggestion only; nothing is applied. */
  action: "needs_level" | "keep_out" | "none";
  level: "use" | "edit" | null;
  knowledge_bases: GrantReportKnowledgeBase[];
  /** Further knowledge bases you can see that the grant also covers. */
  more: number;
  /** A scope grant covers every knowledge base of its home, including ones you cannot see. */
  whole_home: boolean;
  /** A listed knowledge base lets its whole Space or Team in. */
  open_audience: boolean;
  reason: GrantReportMappingReason | null;
}

export interface GrantReportItem {
  grant_id: string;
  effect: "allow" | "deny" | string;
  permission: string;
  expires_at: string | null;
  expired: boolean;
  created_at: string;
  applies_to: GrantReportPrincipal;
  target: GrantReportTarget;
  restriction: GrantReportRestriction;
  mapping: GrantReportMapping;
}

export interface GrantReport {
  /** `BOWHEAD_RESOURCE_GRANTS_ENFORCE=1`: refusals apply now; otherwise they are only logged. */
  enforced: boolean;
  items: GrantReportItem[];
  total: number;
  next_cursor: string | null;
}

export const GRANT_REPORT_PAGE_SIZE = 50;

export async function fetchKnowledgeGrantReport(opts: { cursor?: string | null; limit?: number } = {}): Promise<GrantReport> {
  const qs = new URLSearchParams({ limit: String(opts.limit ?? GRANT_REPORT_PAGE_SIZE) });
  if (opts.cursor) qs.set("cursor", opts.cursor);
  const response = await fetch(`/api/v1/resource-grants/knowledge-base-report?${qs.toString()}`, { credentials: "include" });
  if (!response.ok) throw await readApiError(response);
  return (await response.json()) as GrantReport;
}
