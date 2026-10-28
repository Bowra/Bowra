import { withoutNextRouterHistoryMarkers } from "./url-session-nav";

export const AGENT_CREATION_HANDOFF_KEY = "bowhead.agent-creation-handoff.v1";
export const AGENT_CREATION_DRAFT_PARAM = "agent_draft";

export function agentCreationDraftId(params: URLSearchParams): string | null {
  const draftId = params.get(AGENT_CREATION_DRAFT_PARAM);
  return draftId && /^sad_[a-z0-9]{1,64}$/.test(draftId) ? draftId : null;
}

export function showAgentCreationDraftInUrl(draftId: string | null): void {
  const url = new URL(window.location.href);
  if (draftId) url.searchParams.set(AGENT_CREATION_DRAFT_PARAM, draftId);
  else url.searchParams.delete(AGENT_CREATION_DRAFT_PARAM);
  window.history.replaceState(
    withoutNextRouterHistoryMarkers(window.history.state),
    "",
    `${url.pathname}${url.search}${url.hash}`,
  );
}

export function takeAgentCreationHandoff(): string | null | undefined {
  try {
    const raw = sessionStorage.getItem(AGENT_CREATION_HANDOFF_KEY);
    if (raw === null) return undefined;
    sessionStorage.removeItem(AGENT_CREATION_HANDOFF_KEY);
    return raw.slice(0, 2000).trim() || null;
  } catch {
    return undefined;
  }
}
