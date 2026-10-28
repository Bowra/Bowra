import type { AgentCatalogCollection, AgentCatalogItem } from "@/lib/agents-api";
import type { AgentRankingCollection } from "@/lib/agent-rankings-api";

export const CHART_AGENT: AgentCatalogItem = {
  kind: "agent_catalog_item", schema_version: 1, agent_id: "osa_finance", display_name: "Finance Partner",
  summary: "Explains monthly business performance.", source: { kind: "team", owner_label: "Avery", owner_user_id: "usr_maintainer" },
  facets: { domains: ["finance"], jobs: ["analysis"], outputs: ["report"] }, capabilities: [],
  risk: { level: "low", reasons: [] }, safety: { status: "pass", checked_subject: "revision", reasons: [] },
  assignment: { status: "active", source: "user_hired", row_version: 1 }, team_default: { enabled: false, policy_version: null, row_version: null },
  availability: { discoverable: true, hireable: true, runnable: true, reasons: [] },
  actions: { view_detail: true, hire: false, enable: false, disable: true, unhire: true, manage: false, manage_team_default: false, create: false },
  action_reasons: {}, relationship: "team", updated_label: { kind: "saved_at", at: "2026-09-29T00:00:00Z" }, manager_url: null,
};

export const CHART_CATALOG: AgentCatalogCollection = {
  kind: "agent_catalog_collection", schema_version: 1,
  items: [{ ...CHART_AGENT, agent_id: "osa_directory_first", display_name: "Directory First" }],
  collections: [{ id: "recently_updated", label_key: "collection_recently_updated", agent_ids: [CHART_AGENT.agent_id], items: [CHART_AGENT] }],
  facets: { capabilities: [], domains: [{ id: "finance", count: 1 }, { id: "general", count: 1 }] },
  actions: { create: false }, next_cursor: null,
};

export function chartResult(kind: "popular" | "trending" = "popular", domain: string | null = null): AgentRankingCollection {
  return {
    kind: "agent_ranking_collection", schema_version: 1, ranking_kind: kind, domain_id: domain, state: "ready",
    generated_at: "2026-09-29T00:00:00Z", window_start: kind === "popular" ? "2026-09-01T00:00:00Z" : "2026-09-22T00:00:00Z",
    window_end: "2026-09-29T00:00:00Z", comparison_window_start: kind === "popular" ? null : "2026-09-15T00:00:00Z",
    comparison_window_end: kind === "popular" ? null : "2026-09-22T00:00:00Z",
    rows: [{ display_rank: 1, item: CHART_AGENT, hire_members_current: 20, active_members_28d: 18, active_members_7d: 15, active_members_previous_7d: 7 }],
  };
}
