/**
 * Explicit "open what Coach changed" requests from Chat receipt rows. The page owns the Canvas and re-checks
 * the receipt's user, Team, Space and session before opening; a request grants no access by itself.
 *
 * A row from an "open" event names that event (`requestId`). A row read from the run record has no open
 * event, so it names the Turn's `agent_change` reference instead; the page accepts it only when the same
 * reference was projected for that Turn (slices 2-5 tech design §6).
 */
import {
  coachChangeEffects,
  parseCoachOperationReference,
  type CoachChangeReference,
  type CoachOperationOpen,
} from "./coach-operations";
import { randomUuid } from "./random-id";

export type CoachOperationOpenRequest =
  | { turnId: string; requestId: string }
  | { turnId: string; reference: CoachChangeReference };

const listeners = new Set<(request: CoachOperationOpenRequest) => void>();

export function subscribeCoachOperationRequests(listener: (request: CoachOperationOpenRequest) => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export type CoachReceiptScope = Pick<CoachOperationOpen, "userId" | "teamId" | "workspaceId" | "sessionId">;

/**
 * The pointer a recorded receipt opens with: only for a reference projected for that Turn, in the page's
 * current receipt scope, which the page's open path checks again like an "open" event's pointer.
 */
export function recordedChangePointer(
  request: { turnId: string; reference: CoachChangeReference },
  scope: CoachReceiptScope | null,
): CoachOperationOpen | null {
  const { reference } = request;
  if (!scope || !coachChangeEffects(request.turnId).some((effect) => effect.reference.agent_id === reference.agent_id
    && effect.reference.proposal_id === reference.proposal_id)) return null;
  return {
    requestId: randomUuid(),
    turnId: request.turnId,
    ...scope,
    reference: { kind: "agent_change", agent_id: reference.agent_id, proposal_id: reference.proposal_id },
  };
}

/** Returns whether a page was listening. */
export function requestCoachOperationOpen(request: CoachOperationOpenRequest): boolean {
  if (!request.turnId) return false;
  if ("reference" in request) {
    if (parseCoachOperationReference(request.reference)?.kind !== "agent_change") return false;
  } else if (!/^[0-9a-f]{32}$/.test(request.requestId)) return false;
  for (const listener of listeners) listener(request);
  return listeners.size > 0;
}
