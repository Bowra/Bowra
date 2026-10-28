import { apiFetch, readApiError } from "./api-client";
import { record } from "./coach-operations";
import { spaceHeader } from "./space-storage";
import type { AgentWorkingCopyDetail } from "./agents-api";
import type { TeamPlugin } from "./team-plugins-api";
import type { SkillDrawerTarget } from "@/components/skill-drawer";

export type CoachResourceReference = { kind: "skill" | "tool" | "capability" | "agent"; id: string; revision: string; source_kind?: "user" | "team" | "builtin" };
export type CoachResourceParent = { kind: "agent_draft"; id: string; version: number } | {
  kind: "agent_working_copy"; agent_id: string; working_copy_id: string; version: number; base_release_id: string | null;
};
export type CoachResourceOpen = {
  requestId: string; turnId: string; userId: string; teamId: string; workspaceId: string; sessionId: string;
  reference: CoachResourceReference; parent: CoachResourceParent | null;
};
export function parseResourceReference(value: unknown): CoachResourceReference | null {
  const item = record(value);
  if (!Object.keys(item).every((key) => ["kind", "id", "revision", "source_kind"].includes(key))
    || !["skill", "tool", "capability", "agent"].includes(String(item.kind))
    || ![item.id, item.revision].every((part) => typeof part === "string" && part.length > 0 && part.length <= 512)
    || item.kind === "skill" && !["user", "team", "builtin"].includes(String(item.source_kind))
    || item.source_kind != null && !["user", "team", "builtin"].includes(String(item.source_kind))) return null;
  return { kind: item.kind as CoachResourceReference["kind"], id: item.id as string, revision: item.revision as string,
    ...(item.source_kind ? { source_kind: item.source_kind as CoachResourceReference["source_kind"] } : {}) };
}
export function parseResourceParent(value: unknown): CoachResourceParent | null {
  const item = record(value);
  if (!Number.isSafeInteger(item.version) || Number(item.version) < 1) return null;
  if (item.kind === "agent_draft" && Object.keys(item).every((key) => ["kind", "id", "version"].includes(key))
    && typeof item.id === "string" && /^sad_[A-Za-z0-9_-]{1,76}$/.test(item.id)) return item as CoachResourceParent;
  if (item.kind === "agent_working_copy" && Object.keys(item).every((key) => ["kind", "agent_id", "working_copy_id", "version", "base_release_id"].includes(key))
    && [item.agent_id, item.working_copy_id].every((part) => typeof part === "string" && part.length > 0 && part.length <= 160)
    && (item.base_release_id === null || typeof item.base_release_id === "string" && item.base_release_id.length <= 160)) return item as CoachResourceParent;
  return null;
}
const opens = new Map<string, readonly CoachResourceOpen[]>();
const listeners = new Set<() => void>();
const EMPTY: readonly CoachResourceOpen[] = [];
let revision = 0;
export const subscribeCoachResources = (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; };
export const coachResourcesVersion = () => revision;
export const coachResourceOpens = (turnId: string | null) => turnId ? opens.get(turnId) ?? EMPTY : EMPTY;
export function projectCoachResourceOpen(payload: Record<string, unknown>) {
  const reference = parseResourceReference(payload.reference);
  const parent = payload.parent == null ? null : parseResourceParent(payload.parent);
  if (!reference || payload.parent != null && !parent || typeof payload.request_id !== "string" || !/^[0-9a-f]{32}$/.test(payload.request_id)
    || !["turn_id", "user_id", "team_id", "workspace_id", "session_id"].every((key) => typeof payload[key] === "string" && String(payload[key]).length > 0 && String(payload[key]).length <= 256)) return;
  const turnId = payload.turn_id as string;
  const previous = opens.get(turnId) ?? EMPTY;
  if (previous.some((item) => item.requestId === payload.request_id)) return;
  opens.set(turnId, [...previous.slice(-63), { reference, parent, requestId: payload.request_id, turnId,
    userId: payload.user_id as string, teamId: payload.team_id as string, workspaceId: payload.workspace_id as string, sessionId: payload.session_id as string }]);
  if (opens.size > 256) opens.delete(opens.keys().next().value!);
  revision += 1;
  listeners.forEach((listener) => listener());
}
export type CoachResourceEditor = { reference: CoachResourceReference; read_only: boolean; title: string } & (
  { editor_kind: "agent"; target: { kind: "agent"; agent_id: string; detail: AgentWorkingCopyDetail } } | { editor_kind: "skill"; target: SkillDrawerTarget } | { editor_kind: "plugin"; target: { kind: "plugin"; plugin: TeamPlugin } }
);
export async function readCoachResourceEditor(pointer: CoachResourceOpen, signal: AbortSignal): Promise<CoachResourceEditor> {
  const response = await apiFetch("/api/v1/agent-coach/resources/editor", { method: "POST", signal, cache: "no-store",
    headers: { "Content-Type": "application/json", ...spaceHeader({ workspaceId: pointer.workspaceId, teamId: pointer.teamId }) },
    body: JSON.stringify({ reference: pointer.reference, parent: pointer.parent, workspace_id: pointer.workspaceId, session_id: pointer.sessionId }) });
  if (!response.ok) throw await readApiError(response);
  const value = record(await response.json());
  const reference = parseResourceReference(value.reference);
  const target = record(value.target);
  if (value.ok !== true || value.schema_version !== 1 || !reference
    || reference.id !== pointer.reference.id || reference.kind !== pointer.reference.kind || reference.source_kind !== pointer.reference.source_kind
    || typeof value.read_only !== "boolean" || typeof value.title !== "string") throw new Error("Invalid resource editor projection");
  if (value.editor_kind === "agent") {
    const detail = record(target.detail);
    if (reference.kind !== "agent" || target.kind !== "agent" || target.agent_id !== reference.id
      || detail.agent_id !== reference.id || typeof record(detail.actions).edit !== "boolean"
      || typeof record(detail.live).release_id !== "string") throw new Error("Invalid Agent editor projection");
  } else if (value.editor_kind === "plugin") {
    const plugin = record(target.plugin);
    if (reference.kind !== "capability" || target.kind !== "plugin" || plugin.id !== reference.id || plugin.team_id !== pointer.teamId
      || typeof plugin.name !== "string" || !Number.isSafeInteger(plugin.row_version) || Number(plugin.row_version) < 1
      || String(plugin.revision) !== reference.revision || !["active", "disabled"].includes(String(plugin.status))
      || !Array.isArray(plugin.tool_refs) || !Array.isArray(plugin.skill_grants)) throw new Error("Invalid Plugin editor projection");
  } else {
    const skill = record(target.skill);
    if (value.editor_kind !== "skill" || reference.kind !== "skill" || typeof skill.name !== "string"
      || reference.source_kind === "team" && (target.kind !== "team" || skill.id !== reference.id || typeof target.canEdit !== "boolean" || typeof target.canManage !== "boolean")
      || reference.source_kind !== "team" && target.kind !== "skill"
      || reference.source_kind === "user" && skill.id !== reference.id
      || reference.source_kind === "builtin" && skill.source_type !== "builtin") throw new Error("Invalid Skill editor projection");
  }
  return value as CoachResourceEditor;
}
