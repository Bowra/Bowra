import type { AgentCatalogItem, AgentManagementItem } from "./agents-api";

/**
 * My Agents rows behind FEATURE_AGENT_RELEASE_BAR (slices 2-5 tech design §5). List rows carry no draft or
 * pending-review field, so the only states shown are the exceptions the row itself states.
 */

export type CreatedFilter = "all" | "disabled" | "unavailable";
export type CreatedSort = "updated" | "name";

/** Search, filter and sort load the remaining pages on first use, up to this many rows (decision SD2). */
export const CREATED_SEARCH_ROW_CAP = 500;

export type RowException = "disabled" | "unavailable" | "archived_by_owner" | null;

export function isCatalogRow(item: AgentManagementItem): item is AgentCatalogItem {
  return item.kind !== "agent_archived_management_item";
}

/** The one exception chip a row shows; rows that work show none. */
export function rowException(item: AgentCatalogItem, view: "created" | "hired" | "shared"): RowException {
  if (item.assignment.status === "personal_disabled" || item.availability.public_reason === "personally_disabled") return "disabled";
  if (view === "hired" && item.lifecycle_state === "archived") return "archived_by_owner";
  if (item.availability.runnable) return null;
  // An Agent the viewer has not added (or removed) is not broken: its own button says what to do.
  const reason = item.availability.public_reason;
  if (reason === "not_hired" || item.assignment.status === "not_hired" || item.assignment.status === "removed") return null;
  return "unavailable";
}

export interface CreatedListQuery {
  query: string;
  filter: CreatedFilter;
  sort: CreatedSort;
}

export const DEFAULT_CREATED_QUERY: CreatedListQuery = { query: "", filter: "all", sort: "updated" };

export function createdQueryActive(value: CreatedListQuery): boolean {
  return value.query.trim() !== "" || value.filter !== "all" || value.sort !== DEFAULT_CREATED_QUERY.sort;
}

function normalized(value: string): string {
  return value.normalize("NFKC").toLocaleLowerCase();
}

function updatedAt(item: AgentCatalogItem): number {
  const value = item.updated_label?.at ? Date.parse(item.updated_label.at) : Number.NaN;
  return Number.isFinite(value) ? value : Number.NEGATIVE_INFINITY;
}

/**
 * 我创建的 after its drafts: Agents that are not archived, searched by name or summary, filtered by their
 * exception and sorted. The names are the localized ones the row shows.
 */
export function createdAgentRows(
  items: readonly AgentManagementItem[],
  value: CreatedListQuery,
  text: { name: (item: AgentCatalogItem) => string; summary: (item: AgentCatalogItem) => string; locale: string },
): AgentCatalogItem[] {
  const needle = normalized(value.query.trim());
  const rows = items.filter(isCatalogRow).filter((item) => item.lifecycle_state !== "archived").filter((item) => {
    if (value.filter === "disabled" && rowException(item, "created") !== "disabled") return false;
    if (value.filter === "unavailable" && rowException(item, "created") !== "unavailable") return false;
    if (!needle) return true;
    return normalized(text.name(item)).includes(needle) || normalized(text.summary(item)).includes(needle);
  });
  const collator = new Intl.Collator(text.locale, { sensitivity: "base", numeric: true });
  return [...rows].sort((left, right) => value.sort === "name"
    ? collator.compare(text.name(left), text.name(right)) || left.agent_id.localeCompare(right.agent_id)
    : updatedAt(right) - updatedAt(left) || collator.compare(text.name(left), text.name(right)));
}

/** Archived Agents listed under 我创建的 (the server includes them there); they move to one pointer line. */
export function archivedCreatedCount(items: readonly AgentManagementItem[]): number {
  return items.filter((item) => isCatalogRow(item) && item.lifecycle_state === "archived").length;
}

/** 我聘用的: runnable rows first; the rest go in the folded 现在用不了 group. */
export function splitHiredRows(items: readonly AgentManagementItem[]): { active: AgentCatalogItem[]; unavailable: AgentCatalogItem[] } {
  const rows = items.filter(isCatalogRow);
  return {
    active: rows.filter((item) => item.availability.runnable),
    unavailable: rows.filter((item) => !item.availability.runnable),
  };
}
