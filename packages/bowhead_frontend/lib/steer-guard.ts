export type SteerGuardFailure =
  | "mock"
  | "not_running"
  | "missing_context"
  | "session_mismatch";

export type SteerGuardResult =
  | { ok: true; workspaceId: string; sessionId: string; turnId: string }
  | { ok: false; reason: SteerGuardFailure };

export interface SteerGuardInput {
  sseMode: "sse" | "mock";
  running: boolean;
  turnId: string | null | undefined;
  activeWorkspaceId: string | null | undefined;
  activeSessionId: string | null | undefined;
  runnerSessionId: string | null | undefined;
  /**
   * The send response updates the runner before the page effect claims its new
   * Session. This is true only for that accepted Home transition; New chat and
   * navigation keep it false so a stale runner can never receive the draft.
   */
  allowRunnerSessionClaim: boolean;
}

export function getSteerTarget({
  sseMode,
  running,
  turnId,
  activeWorkspaceId,
  activeSessionId,
  runnerSessionId,
  allowRunnerSessionClaim,
}: SteerGuardInput): SteerGuardResult {
  if (sseMode === "mock") return { ok: false, reason: "mock" };
  if (!running || !turnId) return { ok: false, reason: "not_running" };
  if (!activeWorkspaceId || !runnerSessionId) {
    return { ok: false, reason: "missing_context" };
  }
  if (!activeSessionId) {
    return allowRunnerSessionClaim
      ? { ok: true, workspaceId: activeWorkspaceId, sessionId: runnerSessionId, turnId }
      : { ok: false, reason: "missing_context" };
  }
  if (runnerSessionId !== activeSessionId) {
    return { ok: false, reason: "session_mismatch" };
  }
  return { ok: true, workspaceId: activeWorkspaceId, sessionId: runnerSessionId, turnId };
}
