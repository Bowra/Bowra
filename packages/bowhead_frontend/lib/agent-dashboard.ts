import { listManagedAgents, type AgentCatalogItem } from "./agents-api";

export class AgentDashboardReadError extends Error {
  constructor(public readonly reason: "forbidden" | "incomplete") {
    super(reason);
  }
}

/** Read the current administrator's safe management projection, never authoring records. */
export async function readAgentDashboard(signal: AbortSignal) {
  const items: AgentCatalogItem[] = [];
  const ids = new Set<string>();
  const cursors = new Set<string>();
  let cursor: string | undefined;
  let total: number | undefined;
  // Bound work for large directories until a server aggregate projection is available.
  for (let page = 0; page < 20; page += 1) {
    signal.throwIfAborted();
    const result = await listManagedAgents("team", { cursor, limit: 100, signal });
    signal.throwIfAborted();
    if (result.view !== "team" || !result.views.team || !result.actions.manage_team_defaults) {
      throw new AgentDashboardReadError("forbidden");
    }
    if (result.kind !== "agent_management_collection" || result.schema_version !== 1
      || (total !== undefined && result.total_count !== total)) {
      throw new AgentDashboardReadError("incomplete");
    }
    total = result.total_count;
    for (const item of result.items) {
      if (item.kind !== "agent_catalog_item" || item.source.kind !== "team" || ids.has(item.agent_id)) {
        throw new AgentDashboardReadError("incomplete");
      }
      ids.add(item.agent_id);
      items.push(item);
    }
    if (!result.next_cursor) return { items, omitted: total !== undefined && total > items.length };
    if (cursors.has(result.next_cursor)) throw new AgentDashboardReadError("incomplete");
    cursors.add(result.next_cursor);
    cursor = result.next_cursor;
  }
  throw new AgentDashboardReadError("incomplete");
}

export function agentListingState(item: AgentCatalogItem): "pass" | "blocked" | "unknown" {
  if (item.listing_safety?.effective_status === "blocked" || item.listing_safety?.deterministic_status === "blocked") return "blocked";
  return item.listing_safety?.effective_status === "pass" ? "pass" : "unknown";
}

export function agentDashboardAttention(item: AgentCatalogItem): "blocked" | "default_access" | null {
  if (agentListingState(item) === "blocked") return "blocked";
  if (item.team_default.enabled && !item.actions.view_detail) return "default_access";
  return null;
}

export function agentDashboardDetailHref(agentId: string): string {
  return `/agents/detail/?${new URLSearchParams({ id: agentId, return_page: "dashboard" })}`;
}

export function agentDashboardReturnHref(search: string): string | null {
  return new URLSearchParams(search).get("return_page") === "dashboard" ? "/agents/dashboard/" : null;
}
