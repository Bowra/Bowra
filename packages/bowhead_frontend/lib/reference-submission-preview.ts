import type { RecoveredReferenceSubmission } from "./composer-submission-recovery";

/**
 * The read-only copy of a reference submission (Skill, Agent or Capability
 * references) that the page shows when the user may need to recover it.
 *
 * The local copy is written before every reference send. While that send's
 * POST is in flight the copy is not a recovery problem, so it stays hidden;
 * a Skill-only send waits for automatic receiver selection and otherwise
 * showed "Saved reference draft" for about two seconds on every normal send.
 */
export interface ReferenceSubmissionPreview {
  scopeKey: string;
  draft: RecoveredReferenceSubmission;
  /** The send that wrote this copy is still waiting for its POST. */
  inFlight: boolean;
}

/**
 * A send is about to POST. A copy already shown for recovery stays visible
 * while the same submission is resent, so the recovery view does not flicker.
 */
export function referencePreviewForPendingSend(
  current: ReferenceSubmissionPreview | null,
  scopeKey: string,
  draft: RecoveredReferenceSubmission,
): ReferenceSubmissionPreview {
  const shownForRecovery = current !== null
    && !current.inFlight
    && current.scopeKey === scopeKey
    && current.draft.clientSubmissionId === draft.clientSubmissionId;
  return { scopeKey, draft, inFlight: !shownForRecovery };
}

/** A copy recovered from browser storage after a reload is shown at once. */
export function referencePreviewForRecoveredSubmission(
  scopeKey: string,
  draft: RecoveredReferenceSubmission,
): ReferenceSubmissionPreview {
  return { scopeKey, draft, inFlight: false };
}

/**
 * The POST is over. An accepted send has already cleared its copy, so a copy
 * that is still present (failed, interrupted or unconfirmed send) is shown.
 */
export function referencePreviewAfterSendSettled(
  current: ReferenceSubmissionPreview | null,
): ReferenceSubmissionPreview | null {
  return current?.inFlight ? { ...current, inFlight: false } : current;
}

export function referencePreviewVisible(
  preview: ReferenceSubmissionPreview | null,
  scopeKey: string,
): preview is ReferenceSubmissionPreview {
  return preview !== null && preview.scopeKey === scopeKey && !preview.inFlight;
}
