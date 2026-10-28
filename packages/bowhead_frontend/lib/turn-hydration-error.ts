import type { SseErrorReason } from "./types";
import { normalizeReasonCode } from "./error-messages";

/** Reconcile replayed non-terminal errors against the later durable Turn end. */
export function reconcileHydratedErrorAfterDone(
  errorReason: SseErrorReason | null,
  doneStatus: string,
): SseErrorReason | null {
  if (
    ["completed", "success"].includes(doneStatus)
    && errorReason?.source === "canvas_op_failed"
  ) {
    return null;
  }
  return errorReason;
}

/** Build the current failed-turn projection even when its event tail was lost. */
export function resolveHydratedTurnError(
  replayed: SseErrorReason | null,
  terminalStatus: string | null | undefined,
  errorKind: string | null | undefined,
): SseErrorReason | null {
  if (replayed) return replayed;
  if (terminalStatus !== "failed" && terminalStatus !== "aborted") return null;
  return {
    source: "turn_failed",
    reasonCode: normalizeReasonCode(errorKind),
  };
}
