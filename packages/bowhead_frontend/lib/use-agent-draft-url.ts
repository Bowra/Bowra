import { useCallback, useEffect, useRef } from "react";

import {
  agentCreationDraftId,
  showAgentCreationDraftInUrl,
  takeAgentCreationHandoff,
} from "./agent-creation-handoff";
import { withoutNextRouterHistoryMarkers } from "./url-session-nav";

/**
 * Home-page Create Agent card/URL coordination: opening with `?agent_draft=` / `?agent_create=1` or a session-storage
 * handoff restores the card; the page writes its draft ID into the URL (refresh returns to the same draft).
 *
 * The page strips Next history markers when writing the URL (see `withoutNextRouterHistoryMarkers`), and Next synchronizes the write into
 * `searchParams`, so restoration sees its own written draft ID. This is an echo, not opening the page with a draft ID: using it to
 * reset loses the card's ongoing state (draft revision, review progress). Closing also clears the page-written draft ID,
 * or any later URL synchronization would reopen the closed card by that ID.
 */
export function useAgentDraftUrl(args: {
  searchParams: URLSearchParams;
  authoringEnabled: boolean;
  cardOpen: boolean;
  onRestoreDraft: (draftId: string) => void;
  onStartCreation: (purpose: string | null) => void;
  scope?: { teamId: string | null; workspaceId: string | null; sessionId: string | null };
  onScopeChange?: () => void;
}): { showDraftInUrl: (draftId: string | null) => void; scopeMatches: boolean } {
  const consumedRef = useRef(false);
  const shownDraftRef = useRef<string | null>(null);
  const cardWasOpenRef = useRef(args.cardOpen);
  const scopeRef = useRef(args.scope);
  const previous = scopeRef.current;
  const next = args.scope;
  const scopeMatches = !previous || !next || (
    (previous.teamId === null || previous.teamId === next.teamId)
    && (previous.workspaceId === null || previous.workspaceId === next.workspaceId)
    && (previous.sessionId === null || previous.sessionId === next.sessionId)
  );
  const onScopeChangeRef = useRef(args.onScopeChange);
  onScopeChangeRef.current = args.onScopeChange;
  const onRestoreDraftRef = useRef(args.onRestoreDraft);
  const onStartCreationRef = useRef(args.onStartCreation);
  onRestoreDraftRef.current = args.onRestoreDraft;
  onStartCreationRef.current = args.onStartCreation;

  const showDraftInUrl = useCallback((draftId: string | null) => {
    shownDraftRef.current = draftId;
    showAgentCreationDraftInUrl(draftId);
  }, []);

  useEffect(() => {
    scopeRef.current = args.scope;
    if (scopeMatches) return;
    const liveDraft = agentCreationDraftId(new URLSearchParams(window.location.search));
    if (liveDraft === shownDraftRef.current) showDraftInUrl(null);
    else shownDraftRef.current = null;
    consumedRef.current = false;
    onScopeChangeRef.current?.();
  }, [args.scope?.teamId, args.scope?.workspaceId, args.scope?.sessionId, scopeMatches, showDraftInUrl]);

  useEffect(() => {
    const wasOpen = cardWasOpenRef.current;
    cardWasOpenRef.current = args.cardOpen;
    if (wasOpen && !args.cardOpen && shownDraftRef.current !== null) {
      if (agentCreationDraftId(new URLSearchParams(window.location.search)) === shownDraftRef.current) showDraftInUrl(null);
      else shownDraftRef.current = null;
    }
  }, [args.cardOpen, showDraftInUrl]);

  const { searchParams, authoringEnabled } = args;
  useEffect(() => {
    const savedDraftId = agentCreationDraftId(searchParams);
    if (consumedRef.current && !savedDraftId) return;
    if (savedDraftId && savedDraftId === shownDraftRef.current) return;
    // Snapshot may lag (Next synchronizes via transition): after closing the card and clearing its draft ID, the old snapshot's ID
    // must not reopen the card. Use the live URL as authority.
    if (savedDraftId && agentCreationDraftId(new URLSearchParams(window.location.search)) !== savedDraftId) return;
    if (savedDraftId && authoringEnabled) {
      consumedRef.current = true;
      shownDraftRef.current = savedDraftId;
      onRestoreDraftRef.current(savedDraftId);
      return;
    }
    if (!authoringEnabled) return;
    let purpose = takeAgentCreationHandoff();
    if (purpose === undefined && searchParams.get("agent_create") === "1") {
      purpose = null;
      try {
        // Remove only entry parameters; replacing the entire URL with `/` loses session parameters too, looking like external navigation home.
        const url = new URL(window.location.href);
        url.searchParams.delete("agent_create");
        window.history.replaceState(
          withoutNextRouterHistoryMarkers(window.history.state),
          "",
          `${url.pathname}${url.search}${url.hash}`,
        );
      } catch { /* ignore */ }
    }
    if (purpose === undefined) return;
    consumedRef.current = true;
    onStartCreationRef.current(purpose);
  }, [searchParams, authoringEnabled]);

  return { showDraftInUrl, scopeMatches };
}
