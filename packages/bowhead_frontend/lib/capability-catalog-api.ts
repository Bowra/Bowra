import { apiFetch, readApiError } from "./api-client";
import type { AgentAuthoringTool } from "./agents-api";

export type DiscoveryTool = Pick<
  AgentAuthoringTool,
  | "stable_ref"
  | "display_name"
  | "description"
  | "display_name_en"
  | "description_en"
  | "display_name_de"
  | "description_de"
  | "display_name_fr"
  | "description_fr"
  | "source_kind"
  | "source_display_name"
  | "risk"
  | "selectable"
  | "locked_reason"
>;
export interface DiscoveryPlugin {
  id: string;
  name: string;
  description: string;
  tool_count: number;
  skill_count: number;
}
export interface CapabilityDiscoveryCatalog {
  tools: DiscoveryTool[];
  /** Read-only resources used by enabled system Agents; not team-selectable IDs. */
  builtin_plugins?: Omit<DiscoveryPlugin, "description">[];
  /** Team plugins; retain the discovery API's compatibility field name. */
  capabilities: DiscoveryPlugin[];
}

export async function fetchCapabilityDiscoveryCatalog(): Promise<CapabilityDiscoveryCatalog> {
  const response = await apiFetch("/api/v1/capability-catalog");
  if (!response.ok) throw await readApiError(response);
  return response.json() as Promise<CapabilityDiscoveryCatalog>;
}
