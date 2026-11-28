import type { KnowledgeBaseAudience, KnowledgeBaseHome } from "./knowledge-bases-api";

/**
 * An Agent's knowledge source policy (`definition.knowledge_access`). Schema 1: everything the user can read in the
 * current context, or selected files. Schema 2 (FEATURE_KNOWLEDGE_BASES): selected knowledge bases, whose files added
 * later count too. Each mode carries only its own selection field.
 */
export interface KnowledgeAccess {
  schema_version: 1 | 2;
  mode: "context_readable" | "selected_files" | "selected_knowledge_bases";
  file_ids?: string[];
  knowledge_base_ids?: string[];
  defaults?: { top_k: number };
}
export interface KnowledgeSourceItem { id: string; name: string; scope: string }
export interface KnowledgeSourceCatalog {
  enabled: boolean;
  available: boolean;
  items: KnowledgeSourceItem[];
  selected_items: KnowledgeSourceItem[];
  total: number;
  max_selected: number;
  top_k_min: number;
  top_k_max: number;
}

/** One knowledge base the editor can use; a default has no name and is named after its home (`knowledgeBaseName`). */
export interface KnowledgeBaseSourceItem {
  id: string;
  is_default: boolean;
  home: KnowledgeBaseHome;
  home_id: string;
  name: string | null;
  audience: KnowledgeBaseAudience;
  audience_level: "use" | "edit";
}
/** My, each Space the editor is a member of (with its name), and the Team. */
export interface KnowledgeBaseSourceGroup {
  home: KnowledgeBaseHome;
  home_id: string;
  name: string | null;
  items: KnowledgeBaseSourceItem[];
}
/**
 * `disabled`: the switch is off (nothing is listed; a saved selection reads nothing). `preparing`: the Team's knowledge
 * bases are being prepared for Agents (the mode waits). `unavailable`: they cannot be read now. `ready`: selectable.
 */
export type KnowledgeBaseSourceState = "disabled" | "preparing" | "unavailable" | "ready";
export interface KnowledgeBaseSourceCatalog {
  enabled: boolean;
  state: KnowledgeBaseSourceState;
  max_selected: number;
  groups: KnowledgeBaseSourceGroup[];
  selected_items: KnowledgeBaseSourceItem[];
}

export const MAX_SELECTED_KNOWLEDGE_BASES = 32;

const POLICY_KEYS = ["schema_version", "mode", "file_ids", "knowledge_base_ids", "defaults"];
const isIdList = (value: unknown): value is string[] => Array.isArray(value) && value.every((id) => typeof id === "string");

export function readKnowledgeAccess(value: unknown): KnowledgeAccess | null | "unsupported" {
  if (value === undefined) return null;
  if (!value || typeof value !== "object") return "unsupported";
  const policy = value as KnowledgeAccess;
  if (Object.keys(policy).some((key) => !POLICY_KEYS.includes(key))) return "unsupported";
  if ("defaults" in policy && (!policy.defaults || typeof policy.defaults !== "object" || Object.keys(policy.defaults).some((key) => key !== "top_k") || ("top_k" in policy.defaults && !Number.isInteger(policy.defaults.top_k)))) return "unsupported";
  if (policy.mode === "selected_knowledge_bases") {
    if (policy.schema_version !== 2 || !isIdList(policy.knowledge_base_ids) || "file_ids" in policy) return "unsupported";
    return policy;
  }
  if (policy.schema_version !== 1 || !["context_readable", "selected_files"].includes(policy.mode) || "knowledge_base_ids" in policy) return "unsupported";
  if (policy.mode === "selected_files" && !isIdList(policy.file_ids)) return "unsupported";
  if (policy.mode === "context_readable" && "file_ids" in policy) return "unsupported";
  return policy;
}

/** The selected knowledge bases of a policy, in a stable order; empty for the other modes. */
export function selectedKnowledgeBaseIds(policy: KnowledgeAccess | null | "unsupported"): string[] {
  return policy && policy !== "unsupported" && policy.mode === "selected_knowledge_bases" ? policy.knowledge_base_ids ?? [] : [];
}
