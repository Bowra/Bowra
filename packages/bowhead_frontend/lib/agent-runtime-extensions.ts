export type SkillSource = "builtin" | "team" | "user";
export type AgentAudienceMode = "private" | "custom" | "teamwide";
export interface OptionalSkillSelector { source_kind: SkillSource; id: string; version?: string | null }
export interface AgentRuntimeExtensions {
  schema_version: 1;
  skill_read: {
    enabled: boolean;
    selection_sources: ["user_explicit"];
    allowed_sources: SkillSource[];
    resource_allowlist: OptionalSkillSelector[];
    require_read_only_contract: true;
    max_selected: number;
  };
}
export interface OptionalSkillItem {
  resource: OptionalSkillSelector & { kind: "skill"; version: string };
  label: string;
  description: string;
  allowed_audiences: AgentAudienceMode[];
}
export interface OptionalSkillCatalog {
  kind: "agent_optional_skill_catalog";
  schema_version: 1;
  agent_id: string;
  items: OptionalSkillItem[];
  next_offset: number | null;
}

const SOURCES = ["builtin", "team", "user"];
const AUDIENCES = ["private", "custom", "teamwide"];
const ID = /^[A-Za-z0-9_.:-]{1,512}$/;
const HASH = /^sha256:[0-9a-f]{64}$/;
function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
function keys(value: Record<string, unknown>, required: string[], optional: string[] = []) {
  return required.every((key) => Object.hasOwn(value, key)) && Object.keys(value).every((key) => required.includes(key) || optional.includes(key));
}
function uniqueStrings(value: unknown, allowed: string[]): value is string[] {
  return Array.isArray(value) && value.length > 0 && value.every((item) => typeof item === "string" && allowed.includes(item)) && new Set(value).size === value.length;
}
function selector(value: unknown): value is OptionalSkillSelector {
  return object(value) && keys(value, ["source_kind", "id"], ["version"])
    && typeof value.source_kind === "string" && SOURCES.includes(value.source_kind)
    && typeof value.id === "string" && ID.test(value.id)
    && (value.version == null || typeof value.version === "string" && HASH.test(value.version));
}
export function optionalSkillKey(value: OptionalSkillSelector): string {
  return `${value.source_kind}:${value.id}`;
}

/** Unknown contracts remain opaque; unrelated edits must preserve their bytes. */
export function readRuntimeExtensions(value: unknown): AgentRuntimeExtensions | null | "unsupported" {
  if (value == null) return null;
  try {
    if (new TextEncoder().encode(JSON.stringify(value)).length > 16 * 1024
      || !object(value) || !keys(value, ["schema_version", "skill_read"]) || value.schema_version !== 1 || !object(value.skill_read)) return "unsupported";
    const rule = value.skill_read;
    const allowedSources = rule.allowed_sources;
    if (!keys(rule, ["enabled", "selection_sources", "allowed_sources", "resource_allowlist", "require_read_only_contract", "max_selected"])
      || typeof rule.enabled !== "boolean" || !uniqueStrings(rule.selection_sources, ["user_explicit"])
      || !uniqueStrings(allowedSources, SOURCES) || rule.require_read_only_contract !== true
      || typeof rule.max_selected !== "number" || !Number.isInteger(rule.max_selected) || rule.max_selected < 1 || rule.max_selected > 3
      || !Array.isArray(rule.resource_allowlist) || rule.resource_allowlist.length > 32 || rule.enabled && rule.resource_allowlist.length === 0
      || !rule.resource_allowlist.every((item) => selector(item) && allowedSources.includes(item.source_kind))
      || new Set(rule.resource_allowlist.map(optionalSkillKey)).size !== rule.resource_allowlist.length) return "unsupported";
    return structuredClone(value) as unknown as AgentRuntimeExtensions;
  } catch { return "unsupported"; }
}

export function emptyRuntimeExtensions(): AgentRuntimeExtensions {
  return { schema_version: 1, skill_read: { enabled: false, selection_sources: ["user_explicit"], allowed_sources: ["builtin", "team"],
    resource_allowlist: [], require_read_only_contract: true, max_selected: 3 } };
}

export function optionalSkillsNeedPrivateAudience(value: unknown, audience: AgentAudienceMode): boolean {
  const policy = readRuntimeExtensions(value);
  return audience !== "private" && policy !== null && policy !== "unsupported" && policy.skill_read.resource_allowlist.some((item) => item.source_kind === "user");
}

export function readOptionalSkillCatalog(value: unknown, agentId: string, offset: number): OptionalSkillCatalog {
  if (!object(value) || !keys(value, ["kind", "schema_version", "agent_id", "items", "next_offset"])
    || value.kind !== "agent_optional_skill_catalog" || value.schema_version !== 1 || value.agent_id !== agentId
    || !Array.isArray(value.items) || value.items.length > 50
    || value.next_offset !== null && (typeof value.next_offset !== "number" || !Number.isInteger(value.next_offset) || value.next_offset <= offset || value.next_offset > 100000)) throw new Error("optional_skill_catalog_invalid");
  for (const item of value.items) {
    if (!object(item) || !keys(item, ["resource", "label", "description", "allowed_audiences"]) || !object(item.resource)) throw new Error("optional_skill_catalog_invalid");
    const { kind, ...ref } = item.resource;
    if (kind !== "skill" || !selector(ref) || typeof ref.version !== "string"
      || typeof item.label !== "string" || !item.label || Array.from(item.label).length > 120
      || typeof item.description !== "string" || Array.from(item.description).length > 400
      || !uniqueStrings(item.allowed_audiences, AUDIENCES)
      || ref.source_kind === "user" && (item.allowed_audiences.length !== 1 || item.allowed_audiences[0] !== "private")) throw new Error("optional_skill_catalog_invalid");
  }
  if (new Set(value.items.map((item) => optionalSkillKey(item.resource))).size !== value.items.length) throw new Error("optional_skill_catalog_invalid");
  return structuredClone(value) as unknown as OptionalSkillCatalog;
}
