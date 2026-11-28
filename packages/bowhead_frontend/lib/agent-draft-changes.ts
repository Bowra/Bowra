import type { AgentChangeProposal, AgentDraftChange, AgentDraftChangeSource } from "./agents-api";
import { isAiOptimizeProposal } from "./agent-optimize";

/** The Configure section that holds a draft field. `more` (更多设置) exists only in the redesigned layout. */
export type AgentConfigSectionKey = "does" | "uses" | "who" | "more";

/**
 * Which Configure layout a section map follows: today's three sections, or the redesigned five behind
 * FEATURE_AGENT_RELEASE_BAR (design §4), where knowledge and optional methods move to 它能用什么 and the output
 * contract to 它做什么 › 高级 (S10).
 */
export type AgentConfigLayout = "legacy" | "v2";

const USES_FIELDS = new Set([
  "/definition/tool_refs",
  "/definition/skill_grants",
  "/definition/skills_json",
  "/definition/tool_refs_json",
  "/definition/capability_ids",
  "/definition/builtin_plugin_ids",
  "/definition/team_tool_connections",
]);
const WHO_FIELDS = new Set(["/definition/knowledge_access", "/definition/runtime_extensions", "/definition/output_schema"]);
const V2_USES_FIELDS = new Set([...USES_FIELDS, "/definition/knowledge_access", "/definition/runtime_extensions"]);

export function draftChangeSection(path: string, layout: AgentConfigLayout = "legacy"): AgentConfigSectionKey {
  if (layout === "v2") {
    if (V2_USES_FIELDS.has(path)) return "uses";
    return path.startsWith("/audience/") ? "who" : "does";
  }
  if (USES_FIELDS.has(path)) return "uses";
  if (WHO_FIELDS.has(path) || path.startsWith("/audience/")) return "who";
  return "does";
}

/** Translation key (agents_marketplace) naming a draft field; unknown fields share one generic label. */
export function draftFieldLabelKey(path: string): string {
  if (path === "/definition/knowledge_access") return "knowledge_sources_title";
  if (path === "/definition/system_prompt") return "version_proposal_field_system_prompt";
  if (path === "/definition/tool_refs" || path === "/definition/tool_refs_json" || path === "/definition/team_tool_connections") return "version_proposal_field_tools";
  if (path === "/definition/skill_grants" || path === "/definition/skills_json") return "version_proposal_field_skills";
  if (path === "/definition/capability_ids" || path === "/definition/builtin_plugin_ids") return "version_proposal_field_plugins";
  if (path === "/definition/primary_capability_ref") return "version_proposal_field_capability";
  if (path === "/definition/output_schema") return "version_proposal_field_output";
  if (path === "/definition/model") return "version_proposal_field_model";
  if (path === "/definition/max_iterations") return "version_proposal_field_iterations";
  if (path === "/definition/capability_protocol_block") return "version_proposal_field_capability_protocol_block";
  // The two listing fields people edit by name keep that name (一句话介绍, not 市场资料).
  if (path === "/listing/description") return "release_description";
  if (path === "/listing/display_name") return "release_display_name";
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
export function summarizeDraftChanges(changes: readonly AgentDraftChange[], layout: AgentConfigLayout = "legacy"): DraftChangeSummary[] {
  const order: AgentConfigSectionKey[] = ["does", "uses", "who"];
  const sections = new Map<AgentConfigSectionKey, Map<DraftChangeTag, number>>();
  for (const change of changes) {
    if (sameValue(change.live, change.draft)) continue;
    const section = draftChangeSection(change.path, layout);
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

/** Who changed a field of the saved draft, for its chip on Configure (你改过 / Coach 改过 / AI 优化过). */
export type DraftFieldChip = "manual" | "coach" | "ai_optimize";

/**
 * The chip a field shows, from the draft's change list: Coach's current value, the Owner's own edit, or nothing
 * (unchanged, or set by another source such as a pipeline). `aiProposalIds` are Coach proposals AI 优化 made.
 */
export function draftFieldChip(changes: readonly AgentDraftChange[] | undefined, path: string, aiProposalIds: ReadonlySet<string>): DraftFieldChip | null {
  const change = changes?.find((item) => item.path === path);
  if (!change || sameValue(change.live, change.draft)) return null;
  if (change.coach?.state === "applied" && !change.coach.conflict) return aiProposalIds.has(change.coach.proposal_id) ? "ai_optimize" : "coach";
  return change.source === "manual" || change.coach ? "manual" : null;
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

/** A proposal whose only change is suggested cases (it names the suite they go to). */
export function isCaseOnlyProposal(proposal: Pick<AgentChangeProposal, "base">): boolean {
  return Boolean(proposal.base.suite_revision_id || proposal.base.initial_suite_name);
}

/**
 * The redesigned page (FEATURE_AGENT_RELEASE_BAR) splits a proposal: field changes wait under Configure › 更多设置
 * › 待处理的建议, suggested cases under 测试用例 › 待采纳的建议 (Q5). AI 优化's own result shows under 工作说明, so
 * its field part is left out of the list and of the count (§3.2 "Persistence"); its cases, if any, still go to
 * 测试用例.
 */
export function pendingFieldProposals(proposals: readonly AgentChangeProposal[]): AgentChangeProposal[] {
  return pendingProposals(proposals).filter((proposal) => proposal.status === "open" && !isCaseOnlyProposal(proposal) && !isAiOptimizeProposal(proposal));
}

/** Proposals with suggested cases still waiting for the Owner's decision. */
export function pendingCaseProposals(proposals: readonly AgentChangeProposal[]): AgentChangeProposal[] {
  return pendingProposals(proposals).filter((proposal) => proposal.suggested_cases.some((item) => item.decision_status === "pending"));
}

/** How many suggested cases wait on 测试用例 (the tab chip "N 条建议"). */
export function pendingSuggestedCaseCount(proposals: readonly AgentChangeProposal[]): number {
  return pendingCaseProposals(proposals).reduce((total, proposal) => total + proposal.suggested_cases.filter((item) => item.decision_status === "pending").length, 0);
}
