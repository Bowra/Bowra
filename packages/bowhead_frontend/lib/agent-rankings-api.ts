import { apiFetch, readApiError } from "./api-client";
import type { AgentCatalogItem } from "./agents-api";

export type AgentRankingKind = "popular" | "trending";
export type AgentRankingState = "ready" | "unavailable" | "insufficient_sample" | "privacy_suppressed" | "stale";

export interface AgentRankingRow {
  display_rank: number;
  item: AgentCatalogItem;
  hire_members_current: number;
  active_members_28d?: number;
  active_members_7d?: number;
  active_members_previous_7d?: number;
}

/** Consumer ranking contract; unsupported deployments return null. */
export interface AgentRankingCollection {
  kind: "agent_ranking_collection";
  schema_version: 1;
  ranking_kind: AgentRankingKind;
  domain_id: string | null;
  state: AgentRankingState;
  reason?: "window_incomplete";
  available_at?: string;
  generated_at: string | null;
  window_start: string | null;
  window_end: string | null;
  comparison_window_start: string | null;
  comparison_window_end: string | null;
  rows: AgentRankingRow[];
}

function countAtLeastFive(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 5;
}

function validRow(row: AgentRankingRow, index: number, kind: AgentRankingKind): boolean {
  const item = row?.item;
  return row?.display_rank === index + 1
    && typeof item?.agent_id === "string"
    && typeof item.display_name === "string"
    && typeof item.summary === "string"
    && ["system", "team"].includes(item.source?.kind)
    && item.availability?.discoverable === true
    && item.actions?.view_detail === true
    && countAtLeastFive(row.hire_members_current)
    && (kind === "popular"
      ? countAtLeastFive(row.active_members_28d)
      : countAtLeastFive(row.active_members_7d)
        && countAtLeastFive(row.active_members_previous_7d)
        && row.active_members_7d > row.active_members_previous_7d);
}

function completeWindow(start: string | null, end: string | null, days: number): boolean {
  if (!start || !end) return false;
  const startTime = Date.parse(start);
  const endTime = Date.parse(end);
  return Number.isFinite(startTime) && Number.isFinite(endTime) && endTime - startTime === days * 86_400_000;
}

export async function listAgentRankings({
  rankingKind,
  domain = "all",
  signal,
}: {
  rankingKind: AgentRankingKind;
  domain?: string;
  signal?: AbortSignal;
}): Promise<AgentRankingCollection | null> {
  const query = new URLSearchParams({ ranking_kind: rankingKind });
  if (domain !== "all") query.set("domain", domain);
  const response = await apiFetch(`/api/v1/agents/rankings?${query}`, { signal, cache: "no-store" });
  if (response.status === 404 || response.status === 501) return null;
  if (!response.ok) {
    const error = await readApiError(response);
    if (error.code === "ranking_unavailable") return null;
    throw error;
  }
  const result = await response.json() as AgentRankingCollection;
  if (result?.kind !== "agent_ranking_collection" || result.schema_version !== 1
    || result.ranking_kind !== rankingKind || result.domain_id !== (domain === "all" ? null : domain)
    || !["ready", "unavailable", "insufficient_sample", "privacy_suppressed", "stale"].includes(result.state)) {
    throw new Error("Invalid agent ranking response");
  }
  // Do not retain counts from unavailable or invalidated snapshots, even in client state.
  if (result.state !== "ready") return { ...result, rows: [] };
  const validWindow = completeWindow(result.window_start, result.window_end, rankingKind === "popular" ? 28 : 7)
    && (rankingKind === "popular" || (completeWindow(result.comparison_window_start, result.comparison_window_end, 7)
      && Date.parse(result.comparison_window_end!) === Date.parse(result.window_start!)));
  const validRows = Array.isArray(result.rows) && result.rows.every((row, index) => validRow(row, index, rankingKind))
    && new Set(result.rows.map(row => row.item.agent_id)).size === result.rows.length;
  if (!validWindow || !result.generated_at || !Number.isFinite(Date.parse(result.generated_at)) || !validRows) {
    return { ...result, state: "unavailable", rows: [] };
  }
  return result;
}
