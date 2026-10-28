/**
 * Built-in Plugin and Tool state (Capability Center tech design §8.5–§8.6).
 *
 * Endpoint authorization remains server-side. Readers return `null` on unavailable responses, so
 * pages fall back to the read-only catalog instead of showing an error.
 */
import { apiFetch, readApiError } from "./api-client";
import type { Locale } from "./locale";

export type ToolRisk = "read" | "compute" | "artifact_write" | "external_write" | "destructive";

export interface BuiltinPluginState {
  name: string;
  version: number;
  family: "general" | "settings";
  selectable: boolean;
  listing: Partial<Record<Locale, { name: string; description: string }>>;
  tools: string[];
  tool_count: number;
  skill_count: number;
  prompt_modules: string[];
  availability: string[];
  team_disabled: boolean;
  my_disabled: boolean;
  /** Ready while at least one of its Tools is configured (absent on older servers). */
  ready?: boolean;
  unavailable_tools?: string[];
}

export interface BuiltinToolState {
  name: string;
  risk: ToolRisk;
  enabled: boolean;
}

export type ImpactKind = "builtin_plugin" | "team_plugin" | "builtin_tool";

export interface DisableImpact {
  kind: ImpactKind;
  id: string;
  system_agents: { name: string }[];
  team_agents: { id: string; name: string; display_name: string }[];
  scheduled_tasks: { id: string; name: string; agent_id: string }[];
}

async function readOrNull<T>(path: string): Promise<T | null> {
  const response = await apiFetch(path);
  if (response.status === 404) return null;
  if (!response.ok) throw await readApiError(response);
  return response.json() as Promise<T>;
}

async function put(path: string, body: unknown): Promise<void> {
  const response = await apiFetch(path, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw await readApiError(response);
}

export interface BuiltinPluginsView {
  plugins: BuiltinPluginState[];
  /** Team Plugins the current user disabled for themselves. */
  team_plugins_my_disabled: string[];
}

export async function fetchBuiltinPlugins(): Promise<BuiltinPluginsView | null> {
  const data = await readOrNull<BuiltinPluginsView>("/api/v1/builtin-plugins");
  return data ? { plugins: data.plugins, team_plugins_my_disabled: data.team_plugins_my_disabled ?? [] } : null;
}

export async function fetchBuiltinTools(): Promise<BuiltinToolState[] | null> {
  const data = await readOrNull<{ tools: BuiltinToolState[] }>("/api/v1/builtin-tools");
  return data ? data.tools : null;
}

export function setBuiltinPluginTeamState(name: string, disabled: boolean): Promise<void> {
  return put(`/api/v1/builtin-plugins/${encodeURIComponent(name)}/team-state`, { disabled });
}

export function setBuiltinPluginMyState(name: string, disabled: boolean): Promise<void> {
  return put(`/api/v1/builtin-plugins/${encodeURIComponent(name)}/my-state`, { disabled });
}

export function setTeamPluginMyState(id: string, disabled: boolean): Promise<void> {
  return put(`/api/v1/team-capabilities/${encodeURIComponent(id)}/my-state`, { disabled });
}

export function setBuiltinToolTeamState(
  name: string,
  state: { enabled: boolean; acknowledge_risk?: boolean },
): Promise<void> {
  return put(`/api/v1/builtin-tools/${encodeURIComponent(name)}/team-state`, { acknowledge_risk: false, ...state });
}

export async function fetchDisableImpact(kind: ImpactKind, id: string): Promise<DisableImpact | null> {
  return readOrNull<DisableImpact>(`/api/v1/resources/impact?kind=${encodeURIComponent(kind)}&id=${encodeURIComponent(id)}`);
}
