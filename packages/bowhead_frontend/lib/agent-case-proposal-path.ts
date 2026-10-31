export const AGENT_PENDING_ANCHOR = "agent-pending-changes";

/** Existing Owner surfaces; callers navigate with useAppRouter for entry prefixes. */
export function agentCaseProposalPath(agentId: string, pending: boolean): string {
  const params = new URLSearchParams({ agent_id: agentId, section: pending ? "config" : "evaluate" });
  return `/agents/manage/created/?${params}${pending ? `#${AGENT_PENDING_ANCHOR}` : ""}`;
}
