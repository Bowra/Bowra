/** Shared failure severity and recorded issue counts, never proof of a retry. */
import type { StepView } from "./types";

/** Recoverable classification controls severity; it does not establish follow-up dispatch. */
export function isRecoverableStepFailure(s: StepView): boolean {
  if (s.error?.retryable === false || s.error?.diagnostics?.retryable === false) return false;
  const kind = s.error?.uiKind ?? s.error?.kind;
  if (kind === "permanent") return false;
  if (s.state === "partial") return true;
  if (s.state !== "failed") return false;
  return kind === "transient" || kind === "needs_user_input";
}

/** Count recorded failed/partial steps without claiming correction or successful recovery. */
export function countStepIssues(steps: StepView[]): number {
  return steps.filter((s) => s.state === "failed" || s.state === "partial").length;
}
