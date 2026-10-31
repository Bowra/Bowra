import type { CoachOperationOpen } from "./coach-operations";

export type CoachOperationIntent = { scope: string | null; generation: number; sessionId: string | null; submittedText?: string };

/** A live stream alone is not a new user intent: reconnects can replay a backlog. */
export function maySelectCoachOperation(pointer: CoachOperationOpen, state: {
  intent: CoachOperationIntent | undefined;
  scope: string | null;
  generation: number;
  selectedKey: string | null;
  available: boolean;
}): boolean {
  if (!state.available) return false;
  const intent = state.intent;
  if (intent && (intent.scope !== state.scope || intent.generation !== state.generation || intent.sessionId !== pointer.sessionId)) return false;
  return !state.selectedKey || (pointer.source === "live" && Boolean(intent));
}
