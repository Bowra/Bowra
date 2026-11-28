export const AGENT_PENDING_ANCHOR = "agent-pending-changes";
/** 测试用例 › 待采纳的建议 on the redesigned page (FEATURE_AGENT_RELEASE_BAR): suggested cases next to the suite. */
export const AGENT_PENDING_CASES_ANCHOR = "agent-pending-cases";

/** Existing Owner surfaces; callers navigate with useAppRouter for entry prefixes. */
export function agentCaseProposalPath(agentId: string, pending: boolean): string {
  const params = new URLSearchParams({ agent_id: agentId, section: pending ? "config" : "evaluate" });
  return `/agents/manage/created/?${params}${pending ? `#${AGENT_PENDING_ANCHOR}` : ""}`;
}
