/**
 * CCA P3 — team subagents API wrapper.
 *
 * Backend: /api/v1/team-subagents/* (CRUD + /tools picker catalog). All calls are
 * cookie-authed (`credentials: "include"`); member-level authz + licensing + the
 * authorization and entitlement checks are enforced server-side (the FE never
 * re-checks). Mirrors lib/team-tool-sources-api.ts.
 */

import { apiFetch, readApiError } from "./api-client";
import { spaceHeader } from "./space-storage";
import type { SubagentDraftRequestContext } from "./subagent-draft-context";
import type { AgentAuthoringTool } from "./agents-api";
import { staticTranslator } from "./locale-messages";
import { withoutSkillOperationToolRefs } from "./agent-tool-selection";
import { blockedItemLines, parseBlockedItems, type AgentBlockedItem } from "./agent-blocked-items";

// ── types (match the gateway response shapes) ──────────────────────────────

/** A row in the tool-bundle picker: one of the team's enabled teammcp.* tools. */
export interface TeamToolPickerEntry {
  /** the flat tool name the bundle stores, e.g. "teammcp.crm.getDeal". */
  tool_name: string;
  operation_id: string;
  description: string;
  /** Legacy conservative bit: unknown risk is also true. Not a display label. */
  is_write: boolean;
  /** Server-classified risk; absent metadata must not be inferred from is_write. */
  risk?: AgentAuthoringTool["risk"];
  selectable?: boolean;
  locked_reason?: string | null;
  method: string;
  path: string;
}

/** GET /tools — the member-level picker catalog (team tools + bundle-allowed native tools). */
export interface ToolsCatalog {
  /** Team-scoped server capability; absent means text-only creation is disabled. */
  text_only_allowed?: boolean;
  team_tools: TeamToolPickerEntry[];
  native_tools: string[];
}

/** A skill attachable to an team subagent as an auto-read SOP (the subagent skill_reads it first). */
export interface TeamSubagentSkillEntry {
  name: string;
  display_name?: string;
  display_name_zh?: string;
  description: string;
  description_en?: string;
  description_zh?: string;
  description_de?: string;
  description_fr?: string;
  source_kind?: "builtin" | "team" | "user";
}

/** GET /skills — the picker catalog of attachable SOP skills (builtin catalog + team_wide team skills). */
export interface SkillsCatalog {
  builtin: TeamSubagentSkillEntry[];
  team_skills: TeamSubagentSkillEntry[];
}

export interface SubagentActions {
  view_consumer_detail: boolean;
  view_manager_detail: boolean;
  edit: boolean;
  change_status: boolean;
  manage_audience: boolean;
  transfer_owner: boolean;
  delete: boolean;
  validate: boolean;
  bring_to_web_chat: boolean;
  govern: boolean;
}

export interface SubagentOwnerSummary {
  id: string | null;
  display_name: string | null;
  status: "active" | "inactive" | "unknown";
}

/** Field-safe row returned by list and the Release-A default detail. */
export interface TeamSubagentSummary {
  schema_version: 1;
  kind: "team_subagent_summary";
  id: string;
  name: string;
  display_name: string;
  description: string;
  source: "team";
  owner: SubagentOwnerSummary;
  audience: "private" | "custom" | "team_wide";
  audience_counts: {
    users: number;
    groups: number;
    total: number;
  };
  share_count: number;
  visibility: string;
  status: string;
  lifecycle_state: string;
  integrity: {
    status: string;
    issue_count: number | null;
  };
  availability: {
    web: "available" | "unavailable" | "unknown";
    reason: string | null;
  };
  invocation_mode: string;
  tool_count: number;
  skill_count: number;
  capability_classes: {
    team_tools: number;
    native_tools: number;
    skills: number;
  };
  model: string | null;
  updated_at: string;
  version: number;
  etag: string;
  actions: SubagentActions;
  action_reasons: Record<string, string>;
  access_reason: string | null;
}

/** Full editable definition returned only by explicit ?detail=manager. */
export interface TeamSubagentOwnerManagerDetail {
  schema_version: 1;
  kind: "team_owner_manager_detail";
  /** Old servers omit this; the manager falls back to its release read probe. */
  definition_mode?: "legacy_mutable" | "revisioned";
  id: string;
  team_id: string;
  name: string;
  display_name: string;
  description: string;
  system_prompt: string;
  /** declared bundle: teammcp.<src>.<op> names + native tool names. */
  tool_refs: string[];
  tool_count: number;
  max_iterations: number;
  skills: string[];
  /** allowlisted llm_agent_type (null = default "analyst"). */
  model: string | null;
  /** optional user JSON Schema gating the subagent's return payload (null = none). */
  output_schema: Record<string, unknown> | null;
  visibility: string;
  status: string;
  created_by: string | null;
  created_at: string;
  updated_at: string;
  source: "team";
  owner: SubagentOwnerSummary;
  audience: {
    mode: "private" | "custom" | "team_wide";
    roster: {
      principal_type: "user" | "group";
      principal_id: string;
      principal_name?: string | null;
      permission: string;
    }[];
  };
  lifecycle_state: string;
  version: number;
  etag: string;
  actions: SubagentActions;
  action_reasons: Record<string, string>;
}

export interface CreateSubagentBody {
  name: string;
  display_name?: string;
  description?: string;
  system_prompt?: string;
  tool_refs: string[];
  skills?: string[];
  visibility?: "team_wide" | "creator_only";
  model?: string | null;
  output_schema?: Record<string, unknown> | null;
}

export interface PatchSubagentBody {
  display_name?: string;
  description?: string;
  system_prompt?: string;
  tool_refs?: string[];
  skills?: string[];
  model?: string | null;
  output_schema?: Record<string, unknown> | null;
}

export interface SetSubagentAudienceBody {
  mode: "private" | "custom" | "team_wide";
  user_ids: string[];
  group_ids: string[];
}

// ── error + fetch helpers ──────────────────────────────────────────────────

export class TeamSubagentApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public code?: string,
    /** Dependencies that block saving or release, each with its reasons. */
    public blockedItems: AgentBlockedItem[] = [],
  ) {
    super(message);
    this.name = "TeamSubagentApiError";
  }
}

async function _check(r: Response): Promise<Response> {
  if (r.ok) return r;
  const error = await readApiError(r);
  const detail = _isObject(error.body) ? error.body.detail : null;
  const reasonCodes = _isObject(detail) && Array.isArray(detail.reason_codes)
    ? detail.reason_codes.filter((code): code is string => typeof code === "string")
    : [];
  const toolRef = _isObject(detail) && typeof detail.tool_ref === "string"
    ? detail.tool_ref : null;
  const blockedItems = parseBlockedItems(error.body);
  const blockedMessage = blockedItems.length
    ? blockedItemLines(blockedItems, staticTranslator("agents_marketplace")).join("\n")
    : null;
  throw new TeamSubagentApiError(
    error.status,
    blockedMessage ?? teamSubagentErrorMessage(error.code, reasonCodes, toolRef) ?? error.message,
    error.code,
    blockedItems,
  );
}

/** Codes matching same-named `team_subagent_errors` message keys one to one, looked up directly by code. */
const CODE_MESSAGE_KEYS = new Set([
  "display_name_unavailable",
  "subagent_name_unavailable",
  "agent_delete_schedule_conflict",
  "builtin_subagent_removed",
  "builtin_override_version_conflict",
  "builtin_if_match_required",
  "builtin_override_store_unavailable",
]);

function teamSubagentErrorMessage(code?: string, reasonCodes: string[] = [], toolRef: string | null = null): string | null {
  if (!code) return null;
  const t = staticTranslator("team_subagent_errors");
  if (code === "subagent_tool_not_selectable" && toolRef === "skill_list") {
    return t("skill_list_not_selectable");
  }
  if (code === "subagent_tool_not_selectable") {
    return t("tool_not_selectable");
  }
  if (code === "agent_dependency_blocked") {
    return reasonCodes.includes("precise_skill_scope_required")
      ? t("dependency_precise_scope_required")
      : t("dependency_blocked");
  }
  return CODE_MESSAGE_KEYS.has(code) ? t(code) : null;
}

const BASE = "/api/v1/team-subagents";

function _isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function _expectDto<T>(
  value: unknown,
  kind: string,
): T {
  if (
    !_isObject(value) ||
    value.schema_version !== 1 ||
    value.kind !== kind
  ) {
    throw new TeamSubagentApiError(502, "Unsupported Subagent response");
  }
  return value as T;
}

// ── calls ──────────────────────────────────────────────────────────────────

/** The member-level picker catalog: the team's enabled teammcp.* tools + allowed native tools. */
export async function getToolsCatalog(context?: SubagentDraftRequestContext): Promise<ToolsCatalog> {
  const r = await apiFetch(`${BASE}/tools`, context ? { headers: spaceHeader(context) } : undefined);
  await _check(r);
  const data = (await r.json()) as Partial<ToolsCatalog>;
  return {
    text_only_allowed: data.text_only_allowed === true,
    team_tools: data.team_tools ?? [],
    native_tools: withoutSkillOperationToolRefs(data.native_tools ?? []),
  };
}

/** The picker catalog of skills attachable as auto-read SOPs (builtin catalog + team_wide team skills). */
export async function getSkillsCatalog(context?: SubagentDraftRequestContext): Promise<SkillsCatalog> {
  const r = await apiFetch(`${BASE}/skills`, context ? { headers: spaceHeader(context) } : undefined);
  await _check(r);
  const data = (await r.json()) as Partial<SkillsCatalog>;
  return { builtin: data.builtin ?? [], team_skills: data.team_skills ?? [] };
}

export async function listSubagents(): Promise<TeamSubagentSummary[]> {
  const r = await apiFetch(BASE);
  await _check(r);
  const data = await r.json() as unknown;
  if (
    !_isObject(data) ||
    data.schema_version !== 1 ||
    data.kind !== "team_subagent_summary_list" ||
    !Array.isArray(data.items)
  ) {
    throw new TeamSubagentApiError(502, "Unsupported Subagent list response");
  }
  return data.items.map((item) =>
    _expectDto<TeamSubagentSummary>(item, "team_subagent_summary"));
}

export async function getSubagentManager(
  id: string,
  context?: SubagentDraftRequestContext,
): Promise<TeamSubagentOwnerManagerDetail> {
  const r = await apiFetch(
    `${BASE}/${encodeURIComponent(id)}?detail=manager`,
    context ? { headers: spaceHeader(context) } : undefined,
  );
  await _check(r);
  return _expectDto<TeamSubagentOwnerManagerDetail>(
    await r.json() as unknown,
    "team_owner_manager_detail",
  );
}

/** followup-context-deck #12: system **built-in** subagents (manifest peers, code/manifest-defined).
 *  Separate from team-created list, so users see system-supplied subagents.
 *
 *  Built-in management: fields are **effective values** (code defaults + team overrides). `defaults` are code
 *  defaults; `overridden_fields` identifies team changes. Together they support default/current comparison
 *  and Restore defaults preview, without a frontend-maintained baseline (which would need updates on every manifest change). */
export type BuiltinOverrideField =
  | "enabled"
  | "display_name"
  | "description"
  | "model"
  | "skills"
  | "prompt_append"
  | "primary_capability";

export interface BuiltinSubagent {
  name: string;
  enabled: boolean;
  /** Model tier (routing identifier: analyst / narrative / modeler / echo). */
  model: string;
  /** The model this tier resolves to in the team's configuration; null/absent when unknown. */
  model_display_name?: string | null;
  description: string;
  display_name: string;
  max_iterations: number;
  primary_capability: string | null;
  primary_capability_options?: string[];
  skills: string[];
  prompt_append: string;
  defaults: {
    model: string;
    description: string;
    max_iterations: number;
    primary_capability?: string | null;
  };
  overridden_fields: string[];
  /** Fields a Team may change for this Agent (its manifest `listing.team_overrides`); the page shows only these. */
  team_overrides?: BuiltinOverrideField[];
  /** Team's **raw** delta (null = unchanged). ⚠ Write this back, never the effective values above:
   *  effective `description` passed through sanitization (inducing language replaced with placeholders, NFKC/whitespace normalization);
   *  writing it back would permanently replace administrator's original text with sanitized output. */
  override: {
    display_name: string | null;
    description: string | null;
    model: string | null;
    skills: string[] | null;
    prompt_append: string | null;
    max_iterations?: number | null;
    primary_capability?: string | null;
  } | null;
  row_version: number;
  etag: string;
  /** Non-null means disabling this built-in loses a team capability; UI must confirm again and explain consequences. */
  disable_warning: string | null;
}

export interface BuiltinSubagentList {
  items: BuiltinSubagent[];
  /** false = flag off / not wired: UI falls back to read-only, rendering no edit entry points. */
  manageable: boolean;
}

/** Final team override for one built-in (full write, not PATCH). null means no override for that field. */
export interface BuiltinSubagentOverrideBody {
  enabled: boolean;
  display_name?: string | null;
  description?: string | null;
  model?: string | null;
  skills?: string[] | null;
  prompt_append?: string | null;
  primary_capability?: string | null;
}

export async function listBuiltinSubagents(): Promise<BuiltinSubagentList> {
  // item.etag is the CAS precondition for subsequent PUT; never read it from browser cache. Server also returns
  // Cache-Control: no-store, private; both sides prevent stale lists causing repeated 409s.
  const r = await apiFetch(`${BASE}/builtins`, { cache: "no-store" });
  await _check(r);
  const data = (await r.json()) as Partial<BuiltinSubagentList>;
  return { items: data.items ?? [], manageable: Boolean(data.manageable) };
}

/** Team admin overrides a built-in. `etag` comes from list item (first write uses `"builtin:<name>:0"`);
 *  mismatch → 409; caller should refetch list, not retry. */
export async function putBuiltinSubagentOverride(
  name: string,
  body: BuiltinSubagentOverrideBody,
  etag: string,
): Promise<BuiltinSubagent> {
  const r = await apiFetch(`${BASE}/builtins/${encodeURIComponent(name)}/override`, {
    method: "PUT",
    headers: { "Content-Type": "application/json", "If-Match": etag },
    body: JSON.stringify(body),
  });
  await _check(r);
  return (await r.json()) as BuiltinSubagent;
}

/** Restore defaults = delete override row. Idempotent (unchanged still returns effective values). */
export async function clearBuiltinSubagentOverride(
  name: string,
): Promise<BuiltinSubagent> {
  const r = await apiFetch(`${BASE}/builtins/${encodeURIComponent(name)}/override`, {
    method: "DELETE",
  });
  await _check(r);
  return (await r.json()) as BuiltinSubagent;
}

export async function createSubagent(
  body: CreateSubagentBody,
): Promise<TeamSubagentOwnerManagerDetail> {
  const r = await apiFetch(BASE, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  await _check(r);
  return _expectDto<TeamSubagentOwnerManagerDetail>(
    await r.json() as unknown,
    "team_owner_manager_detail",
  );
}

export async function patchSubagent(
  id: string,
  body: PatchSubagentBody,
  etag: string,
): Promise<TeamSubagentOwnerManagerDetail> {
  const r = await apiFetch(`${BASE}/${encodeURIComponent(id)}/configuration`, {
    method: "PATCH",
    headers: {
      "Content-Type": "application/json",
      "If-Match": etag,
    },
    body: JSON.stringify(body),
  });
  await _check(r);
  return _expectDto<TeamSubagentOwnerManagerDetail>(
    await r.json() as unknown,
    "team_owner_manager_detail",
  );
}

export async function setSubagentStatus(
  id: string,
  status: "enabled" | "disabled",
  etag: string,
): Promise<TeamSubagentOwnerManagerDetail> {
  const r = await apiFetch(`${BASE}/${encodeURIComponent(id)}/status`, {
    method: "PUT",
    headers: {
      "Content-Type": "application/json",
      "If-Match": etag,
    },
    body: JSON.stringify({
      status,
      acknowledged_warning_codes: [],
    }),
  });
  await _check(r);
  return _expectDto<TeamSubagentOwnerManagerDetail>(
    await r.json() as unknown,
    "team_owner_manager_detail",
  );
}

export async function setSubagentAudience(
  id: string,
  body: SetSubagentAudienceBody,
): Promise<TeamSubagentOwnerManagerDetail> {
  const r = await apiFetch(`${BASE}/${encodeURIComponent(id)}/audience`, {
    method: "PUT",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });
  await _check(r);
  return _expectDto<TeamSubagentOwnerManagerDetail>(
    await r.json() as unknown,
    "team_owner_manager_detail",
  );
}

export async function deleteSubagent(id: string, etag: string): Promise<void> {
  const r = await apiFetch(`${BASE}/${encodeURIComponent(id)}`, {
    method: "DELETE",
    headers: { "If-Match": etag },
  });
  await _check(r);
}
