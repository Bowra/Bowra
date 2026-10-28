/**
 * Explicit "open this document draft in the Canvas" requests from chat surfaces (the revision receipt and the
 * Composer revision bar). The page owns the Canvas and re-checks scope before opening; a request grants no
 * document access and opens nothing outside the current session.
 */
export interface DocumentCanvasRequest {
  sessionId: string;
  jobId: string;
}

const listeners = new Set<(request: DocumentCanvasRequest) => void>();

export function subscribeDocumentCanvasRequests(listener: (request: DocumentCanvasRequest) => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Returns whether a page was listening. */
export function requestDocumentCanvas(request: DocumentCanvasRequest): boolean {
  if (!/^djob_[A-Za-z0-9_]{1,123}$/.test(request.jobId) || !request.sessionId) return false;
  for (const listener of listeners) listener(request);
  return listeners.size > 0;
}
