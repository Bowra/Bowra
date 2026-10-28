import { apiFetch, readApiError } from "./api-client";
import { spaceHeader } from "./space-storage";
import type { SubagentDraftRequestContext } from "./subagent-draft-context";
import type { AgentExactSkillGrant } from "./agents-api";

export interface TeamPlugin {
  id: string;
  team_id: string;
  name: string;
  description: string;
  status: "active" | "disabled";
  revision: number;
  row_version: number;
  content_hash: string;
  tool_refs: string[];
  skill_grants: AgentExactSkillGrant[];
  agent_count: number;
}

export interface TeamPluginInput {
  name: string;
  description: string;
  tool_refs: string[];
  skill_grants: AgentExactSkillGrant[];
}

async function result<T>(response: Response): Promise<T> {
  if (!response.ok) throw await readApiError(response);
  return response.json() as Promise<T>;
}

export async function listTeamPlugins(context?: SubagentDraftRequestContext): Promise<TeamPlugin[]> {
  return result(await apiFetch("/api/v1/team-capabilities", context ? { headers: spaceHeader(context) } : undefined));
}

export async function createTeamPlugin(input: TeamPluginInput): Promise<TeamPlugin> {
  return result(await apiFetch("/api/v1/team-capabilities", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(input) }));
}

export async function updateTeamPlugin(id: string, rowVersion: number, input: TeamPluginInput): Promise<TeamPlugin> {
  return result(await apiFetch(`/api/v1/team-capabilities/${encodeURIComponent(id)}`, {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...input, row_version: rowVersion }),
  }));
}

export async function setTeamPluginEnabled(id: string, rowVersion: number, enabled: boolean): Promise<TeamPlugin> {
  return result(await apiFetch(`/api/v1/team-capabilities/${encodeURIComponent(id)}/status`, {
    method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ row_version: rowVersion, enabled }),
  }));
}

export async function deleteTeamPlugin(id: string, rowVersion: number): Promise<void> {
  const response = await apiFetch(`/api/v1/team-capabilities/${encodeURIComponent(id)}?row_version=${rowVersion}`, { method: "DELETE" });
  if (!response.ok) throw await readApiError(response);
}
