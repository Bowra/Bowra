/** Shared failure severity for chat steps. */
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
