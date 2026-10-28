/**
 * Explicit "open what Coach changed" requests from Chat receipt rows. The page owns the Canvas and re-checks
 * the receipt's user, Team, Space and session before opening; a request grants no access by itself.
 */
export interface CoachOperationOpenRequest {
  turnId: string;
  requestId: string;
}

const listeners = new Set<(request: CoachOperationOpenRequest) => void>();

export function subscribeCoachOperationRequests(listener: (request: CoachOperationOpenRequest) => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Returns whether a page was listening. */
export function requestCoachOperationOpen(request: CoachOperationOpenRequest): boolean {
  if (!request.turnId || !/^[0-9a-f]{32}$/.test(request.requestId)) return false;
  for (const listener of listeners) listener(request);
  return listeners.size > 0;
}
