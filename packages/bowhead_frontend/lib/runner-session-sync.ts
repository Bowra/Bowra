/**
 * `runner.sessionId → activeSessionId` sync guard (page.tsx adopts the session after the first message).
 *
 * After the first message, runner receives the real sessionId and syncs it to page's activeSessionId, which determines
 * both URL `?session=` and the session receiving the next message.
 *
 * Original problem to guard against: clicking「新工作」clears active to null before runner.sessionId loses
 * its old ID; syncing in that frame restores the old active ID, sending the next new message to the old session. Thus reset
 * suppresses one frame, until runner.sessionId becomes null (or already matches active).
 *
 * Counterexample observed on 116 on 2026-09-24: clicking「新工作」on an already-clean page (runner and active both null)
 * enables suppression, but clearing it requires the sync effect to rerun; neither dependency changed,
 * so the effect never reruns and suppression never clears. The first message's new ID is swallowed as old: active stays empty,
 * URL never gets `?session=` (refresh returns home), and **the second message starts another session, losing context**.
 * Thus suppression is needed only when runner actually holds an old ID.
 */

export interface RunnerSessionSyncState {
  /** Whether the frame syncing runner's old value → active is suppressed. */
  ignoring: boolean;
}

/** Whether clicking「新工作」must suppress the next sync frame: only while runner holds an old session ID. */
export function shouldIgnoreRunnerSessionOnReset(
  runnerSessionId: string | null | undefined,
): boolean {
  return Boolean(runnerSessionId);
}

/**
 * One sync-effect frame. Return the new suppression state and value to write into activeSessionId (null = no write).
 */
export function reconcileRunnerSessionSync(
  state: RunnerSessionSyncState,
  input: {
    runnerSessionId: string | null | undefined;
    activeSessionId: string | null | undefined;
  },
): { ignoring: boolean; adopt: string | null } {
  const runner = input.runnerSessionId ?? null;
  const active = input.activeSessionId ?? null;
  if (state.ignoring) {
    // Clear suppression once runner.sessionId reaches the target (null or matching active); do not sync this frame.
    const settled = !runner || runner === active;
    return { ignoring: !settled, adopt: null };
  }
  if (active === null && runner) return { ignoring: false, adopt: runner };
  return { ignoring: false, adopt: null };
}
