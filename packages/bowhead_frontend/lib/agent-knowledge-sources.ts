export interface KnowledgeAccess {
  schema_version: 1;
  mode: "context_readable" | "selected_files";
  file_ids?: string[];
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
export function readKnowledgeAccess(value: unknown): KnowledgeAccess | null | "unsupported" {
  if (value === undefined) return null;
  if (!value || typeof value !== "object") return "unsupported";
  const policy = value as KnowledgeAccess;
  if (Object.keys(policy).some((key) => !["schema_version", "mode", "file_ids", "defaults"].includes(key))) return "unsupported";
  if ("defaults" in policy && (!policy.defaults || typeof policy.defaults !== "object" || Object.keys(policy.defaults).some((key) => key !== "top_k") || ("top_k" in policy.defaults && !Number.isInteger(policy.defaults.top_k)))) return "unsupported";
  if (policy.schema_version !== 1 || !["context_readable", "selected_files"].includes(policy.mode)) return "unsupported";
  if (policy.mode === "selected_files" && (!Array.isArray(policy.file_ids) || policy.file_ids.some((id) => typeof id !== "string"))) return "unsupported";
  if (policy.mode === "context_readable" && "file_ids" in policy) return "unsupported";
  return policy;
}
