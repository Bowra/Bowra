/**
 * "This page just released (or submitted) a version of an Agent", for other views of the same Agent on the page:
 * the trial chat ends at once when the Agent editor beside it releases the draft, instead of after a reload
 * (2026-11-27 dogfood F25). A tiny in-memory bus: the release requests in `agents-api` publish after the server
 * accepted them, views subscribe and re-read; nothing is stored and the id grants nothing.
 */

const listeners = new Set<(agentId: string) => void>();

export function subscribeAgentReleased(listener: (agentId: string) => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function publishAgentReleased(agentId: string): void {
  for (const listener of [...listeners]) listener(agentId);
}
