import type { AgentCatalogItem, AgentManagementCollection } from "@/lib/agents-api";
import { CHART_AGENT } from "./agent-charts";

export function dashboardAgent(id: string, options: Partial<AgentCatalogItem> = {}): AgentCatalogItem {
  return { ...CHART_AGENT, agent_id: id, listing_safety: { effective_status: "pass", deterministic_status: "pass", semantic_status: "not_run", listing_hash: null, reasons: [] }, ...options };
}

export function dashboardCollection(items: AgentCatalogItem[], nextCursor: string | null = null, total = items.length): AgentManagementCollection {
  return { kind: "agent_management_collection", schema_version: 1, view: "team", items, total_count: total, next_cursor: nextCursor, views: { hired: true, created: true, shared: true, archived: true, team: true }, actions: { create: true, manage_team_defaults: true } };
}
