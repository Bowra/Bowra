import type { AgentChangeProposal, AgentDraftChange, AgentDraftChangeSource } from "./agents-api";

/** The Configure section that holds a draft field. */
export type AgentConfigSectionKey = "does" | "uses" | "who";

const USES_FIELDS = new Set([
  "/definition/tool_refs",
  "/definition/skill_grants",
  "/definition/skills_json",
  "/definition/tool_refs_json",
  "/definition/capability_ids",
  "/definition/builtin_plugin_ids",
  "/definition/team_tool_connections",
]);
const WHO_FIELDS = new Set(["/definition/runtime_extensions", "/definition/output_schema"]);

export function draftChangeSection(path: string): AgentConfigSectionKey {
  if (USES_FIELDS.has(path)) return "uses";
  if (WHO_FIELDS.has(path) || path.startsWith("/audience/")) return "who";
  return "does";
}

/** Translation key (agents_marketplace) naming a draft field; unknown fields share one generic label. */
export function draftFieldLabelKey(path: string): string {
  if (path === "/definition/system_prompt") return "version_proposal_field_system_prompt";
  if (path === "/definition/tool_refs" || path === "/definition/tool_refs_json" || path === "/definition/team_tool_connections") return "version_proposal_field_tools";
  if (path === "/definition/skill_grants" || path === "/definition/skills_json") return "version_proposal_field_skills";
  if (path === "/definition/capability_ids" || path === "/definition/builtin_plugin_ids") return "version_proposal_field_plugins";
  if (path === "/definition/primary_capability_ref") return "version_proposal_field_capability";
  if (path === "/definition/output_schema") return "version_proposal_field_output";
  if (path === "/definition/model") return "version_proposal_field_model";
  if (path === "/definition/max_iterations") return "version_proposal_field_iterations";
  if (path === "/definition/capability_protocol_block") return "version_proposal_field_capability_protocol_block";
  if (path.startsWith("/listing/")) return "version_proposal_field_listing";
  if (path.startsWith("/audience/")) return "version_proposal_field_audience";
  return "version_proposal_field_other";
}

/** Sources shown as tags, in display order; the rest count as "other". */
export const DRAFT_CHANGE_SOURCES = ["coach", "manual", "ci", "feedback"] as const;
export type DraftChangeTag = (typeof DRAFT_CHANGE_SOURCES)[number] | "other";

export function draftChangeTag(source: AgentDraftChangeSource): DraftChangeTag {
  return (DRAFT_CHANGE_SOURCES as readonly string[]).includes(source) ? source as DraftChangeTag : "other";
}

export interface DraftChangeSummary {
  section: AgentConfigSectionKey;
  count: number;
  sources: Array<{ tag: DraftChangeTag; count: number }>;
}

/**
 * Changes that differ from live, per Configure section, with how many each source made. A Coach row the
 * Owner undid back to the live value no longer differs, so it is not counted.
 */
export function summarizeDraftChanges(changes: readonly AgentDraftChange[]): DraftChangeSummary[] {
  const order: AgentConfigSectionKey[] = ["does", "uses", "who"];
  const sections = new Map<AgentConfigSectionKey, Map<DraftChangeTag, number>>();
  for (const change of changes) {
    if (sameValue(change.live, change.draft)) continue;
    const section = draftChangeSection(change.path);
    const counts = sections.get(section) ?? new Map<DraftChangeTag, number>();
    const tag = draftChangeTag(change.source);
    counts.set(tag, (counts.get(tag) ?? 0) + 1);
    sections.set(section, counts);
  }
  return order.filter((section) => sections.has(section)).map((section) => {
    const counts = sections.get(section)!;
    const sources = [...DRAFT_CHANGE_SOURCES, "other" as const]
      .filter((tag) => counts.has(tag))
      .map((tag) => ({ tag, count: counts.get(tag)! }));
    return { section, count: sources.reduce((total, item) => total + item.count, 0), sources };
  });
}

/** Absent and empty (null, "", [] or {}) mean the same, matching the server's draft comparison. */
function sameValue(left: unknown, right: unknown): boolean {
  return JSON.stringify(left ?? null) === JSON.stringify(right ?? null) || (isEmptyValue(left) && isEmptyValue(right));
}

function isEmptyValue(value: unknown): boolean {
  if (value === undefined || value === null || value === "") return true;
  if (Array.isArray(value)) return value.length === 0;
  return typeof value === "object" && Object.keys(value).length === 0;
}

/**
 * Proposals the Owner reviews on Configure. Coach writes the draft directly, so its accepted proposals
 * belong to the change list instead, unless they still carry suggested cases to decide; an open Coach
 * proposal from before that change still needs a decision too.
 */
export function pendingProposals(proposals: readonly AgentChangeProposal[]): AgentChangeProposal[] {
  return proposals.filter((proposal) => proposal.source_type !== "coach"
    || proposal.status === "open"
    || proposal.suggested_cases.some((item) => item.decision_status === "pending"));
}
