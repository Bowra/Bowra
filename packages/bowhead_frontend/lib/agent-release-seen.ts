import type { AgentVersionWorkbench } from "@/lib/agents-api";

/** Primary actions that mean an evaluation result is waiting for the Owner on Release. */
const RESULT_ACTIONS = new Set([
  "activate_candidate",
  "submit_for_review",
  "resolve_risks",
  "fix_next_draft",
  "retry_after_environment_fix",
]);

/** When the waiting result was produced, or null when Release holds nothing for the Owner to act on. */
export function releaseResultAt(workbench: Pick<AgentVersionWorkbench, "primary_action" | "active_snapshot_identity">): string | null {
  const snapshot = workbench.active_snapshot_identity;
  return snapshot && RESULT_ACTIONS.has(workbench.primary_action?.kind) ? snapshot.created_at : null;
}

const seenKey = (agentId: string) => `bowhead.agent-release-seen.${agentId}`;

/** When this viewer last opened Release for the Agent. Per-viewer convenience: unreadable storage means never. */
export function readReleaseSeen(agentId: string): string | null {
  try {
    return window.localStorage.getItem(seenKey(agentId));
  } catch {
    return null;
  }
}

export function writeReleaseSeen(agentId: string, at: string): void {
  try {
    window.localStorage.setItem(seenKey(agentId), at);
  } catch {
    // Private windows and blocked storage only lose the marker's memory, not the result.
  }
}

/** A result is new to this viewer when it was produced after they last opened Release. */
export function releaseResultUnseen(resultAt: string | null, seenAt: string | null): boolean {
  if (!resultAt) return false;
  const produced = Date.parse(resultAt);
  const seen = seenAt ? Date.parse(seenAt) : Number.NaN;
  return !Number.isFinite(seen) || !Number.isFinite(produced) || seen < produced;
}
