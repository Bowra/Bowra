export type ComposerCancelOutcome = {
  status: "cancelled" | "noop";
  ack: "accepted" | "rejected";
  target_state: "present" | "erased";
  durable: boolean;
};

/** Only an exact durable cancellation may close the live stream locally. */
export function shouldApplyComposerCancellation(
  outcome: ComposerCancelOutcome,
): boolean {
  return outcome.durable
    && outcome.status === "cancelled"
    && outcome.ack === "accepted"
    && outcome.target_state === "present";
}

/** A durable non-running fact removes any local Stop recovery journal. */
export function shouldClearComposerCancellation(
  outcome: ComposerCancelOutcome,
): boolean {
  return outcome.durable && (
    outcome.target_state === "erased"
    || outcome.ack === "rejected"
    || outcome.status === "noop"
  );
}
