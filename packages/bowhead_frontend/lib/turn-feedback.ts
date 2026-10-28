/**
 * Chat turn feedback signals (docs/design/chat-turn-feedback-signals-tech-design.md):
 * D3 legacy failure-answer suppression, D5 failure-card recovery action, D6 step outcome.
 *
 * Pure projections of persisted event fields. Live SSE and replay feed the same events
 * through the same reducers, so every view reads the same value (live == persisted == replayed).
 */

import { REASON_CODE_LIST } from "./error-messages";
import type { ErrorDiagnostics } from "./error-presentation";

// ─── D5: recovery action ────────────────────────────────────────────────

export const RECOVERY_ACTIONS = [
  "retry",
  "edit_and_resend",
  "copy_diagnostics",
  "new_session",
] as const;

export type RecoveryAction = (typeof RECOVERY_ACTIONS)[number];

const RECOVERY_ACTION_SET = new Set<string>(RECOVERY_ACTIONS);

/** Accept only the finite backend vocabulary; anything else is "not published". */
export function parseRecoveryAction(value: unknown): RecoveryAction | undefined {
  return typeof value === "string" && RECOVERY_ACTION_SET.has(value)
    ? (value as RecoveryAction)
    : undefined;
}

/**
 * The failure card's primary action. The backend owns the per-reason table and publishes
 * `recovery_action`; old turns without it fall back to the persisted `retryable` fact:
 * true → retry, false → copy diagnostics, unknown → edit and resend. No frontend mirror of the
 * table exists, so there is one implementation.
 */
export function resolveRecoveryAction(
  recoveryAction: RecoveryAction | undefined,
  diagnostics: ErrorDiagnostics | undefined,
): RecoveryAction {
  if (recoveryAction) return recoveryAction;
  if (diagnostics?.retryable === true) return "retry";
  if (diagnostics?.retryable === false) return "copy_diagnostics";
  return "edit_and_resend";
}

export interface RecoveryAvailability {
  /** Re-run the original question through the composer's Replay. */
  retry: boolean;
  /** Replay would only restore the question into the composer (no server replay available). */
  retryRestoresDraft?: boolean;
  /** Put the original question in the composer for editing. */
  editAndResend: boolean;
  /** Open a new session with the original question prefilled. */
  newSession: boolean;
}

/**
 * Exactly one primary action. When the surface cannot perform the published action (for
 * example retry on an older archived turn, or no original text), degrade to editing the
 * question, and finally to copying diagnostics, which is always possible.
 */
export function chooseFailureCardAction(
  action: RecoveryAction,
  available: RecoveryAvailability,
): RecoveryAction {
  // A Replay that only restores the draft is an edit, not a retry: label it as such.
  if (action === "retry" && available.retry && !available.retryRestoresDraft) return "retry";
  if (action === "new_session" && available.newSession) return "new_session";
  if (action === "copy_diagnostics") return "copy_diagnostics";
  if (available.editAndResend) return "edit_and_resend";
  // A question that cannot be edited here (an older attachment-only turn) can still start again with its
  // attachments in a new session: a usable way back beats copying diagnostics.
  if (available.newSession) return "new_session";
  return "copy_diagnostics";
}

// ─── D3: legacy failure answers ─────────────────────────────────────────

const LEGACY_FAILURE_ANSWER_REASONS = new Set<string>([
  ...REASON_CODE_LIST,
  "silent_done_fallback",
]);

/**
 * Before D3, a failed turn also emitted a system failure sentence as `final_answer`, tagged
 * with the public failure `reason_code` or `silent_done_fallback`. Such an answer is hidden
 * once the turn's failure terminal arrives; the failure card is the only failure statement.
 * A normal answer with another reason code (e.g. `canvas_pointer_reconciled`) is unaffected,
 * and a completed turn keeps its `silent_done_fallback` empty-result line.
 */
export function isLegacyFailureAnswerReason(reasonCode: unknown): boolean {
  return typeof reasonCode === "string" && LEGACY_FAILURE_ANSWER_REASONS.has(reasonCode);
}

// ─── D6: step outcome ───────────────────────────────────────────────────

export interface StepOutcome {
  issueSteps: number;
  recoveredSteps: number;
  unresolvedSteps: number;
  answerAffected: boolean;
  /** Whether the carrying `done` reported `status: "completed"`. */
  turnCompleted: boolean;
}

function count(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : null;
}

/** Parse `done.step_outcome`; a missing or malformed field means unknown (null), never zero. */
export function parseStepOutcome(
  value: unknown,
  doneStatus: unknown,
): StepOutcome | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const raw = value as Record<string, unknown>;
  if (raw.schema_version !== 1) return null;
  const issueSteps = count(raw.issue_steps);
  const recoveredSteps = count(raw.recovered_steps);
  const unresolvedSteps = count(raw.unresolved_steps);
  if (issueSteps === null || recoveredSteps === null || unresolvedSteps === null) return null;
  if (typeof raw.answer_affected !== "boolean") return null;
  return {
    issueSteps,
    recoveredSteps,
    unresolvedSteps,
    answerAffected: raw.answer_affected,
    turnCompleted: doneStatus === "completed",
  };
}

export type StepIssueCopyKey =
  | "step_issues_handled_n"
  | "step_issues_affecting_n"
  | "step_issues_n";

export interface StepIssueSummary {
  key: StepIssueCopyKey;
  count: number;
  tone: "secondary" | "warning";
}

/**
 * Step-summary issue copy (D6). Warning tone only when an unresolved issue reached the answer
 * of a completed turn; a missing outcome (old turns) keeps the neutral local count.
 */
export function stepIssueSummary(
  outcome: StepOutcome | null | undefined,
  localIssueCount: number,
): StepIssueSummary | null {
  if (!outcome) {
    return localIssueCount > 0
      ? { key: "step_issues_n", count: localIssueCount, tone: "secondary" }
      : null;
  }
  if (outcome.turnCompleted && outcome.answerAffected && outcome.unresolvedSteps > 0) {
    return { key: "step_issues_affecting_n", count: outcome.unresolvedSteps, tone: "warning" };
  }
  // Later successful calls prove execution recovery, not fulfillment of the
  // original request. Mixed unresolved issues retain the neutral issue count.
  if (outcome.turnCompleted && !outcome.answerAffected && outcome.issueSteps > 0 && outcome.recoveredSteps > 0 && outcome.unresolvedSteps === 0) {
    return { key: "step_issues_handled_n", count: outcome.recoveredSteps, tone: "secondary" };
  }
  // Failed or cancelled turns: the failure card owns that state; keep the neutral count.
  return outcome.issueSteps > 0
    ? { key: "step_issues_n", count: outcome.issueSteps, tone: "secondary" }
    : null;
}
