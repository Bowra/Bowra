/** Shared reason copy for all Agent authoring pickers; the server owns eligibility. */
export function toolSelectionLockedReasonKey(reason: string) {
  if (reason === "approval_executor_unavailable") return "draft_locked_approval_executor";
  if (reason === "mcp_feature_unlicensed") return "draft_locked_mcp";
  if (reason === "risk_metadata_unknown") return "draft_locked_risk_unknown";
  if (reason === "precise_skill_scope_required") return "draft_locked_precise_skill_scope";
  return "draft_locked_unavailable";
}

/**
 * Skill operations are granted by the Skills an Agent includes, not chosen as Tools. The server
 * adds them when it compiles the Agent and ignores stored references, so pickers never show them,
 * even when an older server still lists them.
 */
const SKILL_OPERATION_TOOL_REFS: ReadonlySet<string> = new Set(["skill_read", "skill_invoke", "skill_list"]);

export function isSkillOperationToolRef(ref: string): boolean {
  return SKILL_OPERATION_TOOL_REFS.has(ref);
}

export function withoutSkillOperationToolRefs(refs: readonly string[]): string[] {
  return refs.filter((ref) => !isSkillOperationToolRef(ref));
}

export function withoutSkillOperationTools<T extends { stable_ref: string }>(tools: readonly T[]): T[] {
  return tools.filter((tool) => !isSkillOperationToolRef(tool.stable_ref));
}

/** What an Agent was given, counted by kind: Plugins, Skills and directly picked Tools. */
export interface AgentResourceCounts {
  plugins: number;
  skills: number;
  tools: number;
}

function stringList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string" && item.length > 0) : [];
}

/**
 * One counting rule for the creation draft, the live receipt and the receipt read back after a reload, so the
 * three never disagree. A Plugin counts once as a Plugin, never as the Tools it brings; Tools are the ones picked
 * one by one. The Python Skill grant that picking Run code adds on its own is not a Skill the user picked.
 */
export function agentResourceCounts(definition: {
  tool_refs?: unknown;
  skill_grants?: unknown;
  builtin_plugin_ids?: unknown;
  capability_ids?: unknown;
}): AgentResourceCounts {
  const tools = new Set(withoutSkillOperationToolRefs(stringList(definition.tool_refs)));
  const grants = Array.isArray(definition.skill_grants) ? definition.skill_grants : [];
  const skills = grants.filter((grant) => {
    const name = grant && typeof grant === "object" ? (grant as { skill_ref?: { name?: unknown } }).skill_ref?.name : undefined;
    return !(name === "python_sandbox" && tools.has("python_sandbox"));
  }).length;
  return {
    plugins: new Set([...stringList(definition.builtin_plugin_ids), ...stringList(definition.capability_ids)]).size,
    skills,
    tools: tools.size,
  };
}
