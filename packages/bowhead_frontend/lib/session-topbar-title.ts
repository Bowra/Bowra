export interface SessionTitleItem {
  id: string;
  title: string;
}

function titleForSession(
  sessionId: string,
  items: readonly SessionTitleItem[],
): string | undefined {
  const item = items.find((candidate) => candidate.id === sessionId);
  return item && item.title !== sessionId ? item.title : undefined;
}

/**
 * Resolve the user-facing title for the session currently represented by the page.
 *
 * Navigation can select a session before the turn runner hydrates it. Prefer an
 * optimistic rename, then the remote and local session lists, and only then a
 * runner title that belongs to the same session.
 */
export function resolveSessionTopbarTitle({
  sessionId,
  titleOverrides,
  remoteItems,
  localItems,
  runnerSessionId,
  runnerSessionTitle,
}: {
  sessionId: string | null;
  titleOverrides: ReadonlyMap<string, string>;
  remoteItems: readonly SessionTitleItem[];
  localItems: readonly SessionTitleItem[];
  runnerSessionId: string | null;
  runnerSessionTitle: string | null | undefined;
}): string | undefined {
  if (!sessionId) return undefined;
  return titleOverrides.get(sessionId)
    ?? titleForSession(sessionId, remoteItems)
    ?? titleForSession(sessionId, localItems)
    ?? (runnerSessionId === sessionId ? runnerSessionTitle ?? undefined : undefined);
}

/**
 * The session the title-bar "⋯" menu may act on: the one the title names. While navigation points at a different
 * session than the runner still holds (another session is opening, or a report-only session is shown), the bar
 * names the navigation target, so the menu is withheld rather than archiving or deleting the runner's session
 * under another session's name.
 */
export function sessionTopbarMenuTarget({
  titleSessionId,
  runnerSessionId,
}: {
  titleSessionId: string | null;
  runnerSessionId: string | null;
}): string | null {
  if (!runnerSessionId) return null;
  return titleSessionId === null || titleSessionId === runnerSessionId ? runnerSessionId : null;
}
