/**
 * dogfood-0708 F1 — decide which clarification cards to restore on session hydrate (F5/deep-link).
 *
 * **Pure decision** extracted from page.tsx's `_fetchV4FromTurn` for testing (previously inlined inside an async
 * fetch function and untestable, which let F1 reach production).
 *
 * Contract: restore clarification cards only while the turn **is still paused at them**.
 * - Last done status = `clarification_pending` ⇒ actually paused → keep unresolved cards;
 * - Any terminal state (completed/failed/aborted/dismissed…) ⇒ turn moved on → discard all;
 * - Discard resolved cards (resolvedClarIds) even when paused (keep only the card still awaiting an answer).
 *
 * F1 cause: done{clarification_pending} also stamps response_kind on a turn paused for clarification
 * (the decider runs before pausing) → old `final/responseKind` proxy incorrectly marks turnEnded
 * → the entire card disappears and cannot be answered. Fix: lastDoneStatus is authoritative; ignore the proxy for clarification_pending.
 */

import { listClarificationsForTurn, type ClarificationListDTO } from "./api";
import { normalizeErrorDiagnostics } from "./error-presentation";
import { parseRecoveryAction } from "./turn-feedback";
import type { SseErrorReason } from "./types";

/** A resolved answer can still be blocked before its original continuation. */
export function clarificationResumeFailure(
  failure: ClarificationListDTO["resume_failure"],
  turnId: string,
  generation?: number | null,
): SseErrorReason | null {
  if (!failure || failure.turn_id !== turnId
    || failure.code !== "clarification_resume_unavailable"
    || !failure.request_id || !Number.isInteger(failure.execution_generation)
    || failure.execution_generation < 1
    || (generation != null && failure.execution_generation !== generation)) return null;
  return {
    source: "clarification_resume",
    reasonCode: failure.code,
    diagnostics: normalizeErrorDiagnostics(failure.diagnostics),
    recoveryAction: parseRecoveryAction(failure.recovery_action),
  };
}

export async function readHydratedClarificationAuthority(
  turnId: string,
  candidates: unknown[],
  lastDoneStatus: string | null,
  generation?: number | null,
): Promise<{ clarifications: unknown[]; resumeFailure: SseErrorReason | null }> {
  // The resolved event removes all candidates before resume preflight. A
  // paused Turn still needs the read even when there is no unanswered card.
  if (!candidates.length && lastDoneStatus !== "clarification_pending") {
    return { clarifications: candidates, resumeFailure: null };
  }
  const authority = await listClarificationsForTurn(turnId);
  return {
    clarifications: retainPendingClarifications(candidates, new Set(authority.items.map((item) => item.request_id))),
    resumeFailure: lastDoneStatus === "clarification_pending"
      ? clarificationResumeFailure(authority.resume_failure, turnId, generation) : null,
  };
}

export const TERMINAL_DONE_STATUSES = new Set([
  "completed",
  "failed",
  "aborted",
  "error",
  "clarification_dismissed",
]);

export function clarificationId(c: unknown): string {
  if (!c || typeof c !== "object") return "";
  const r = c as Record<string, unknown>;
  return String(r.clarification_id ?? r.request_id ?? "");
}

/** Intersect replay candidates with the backend's current pending authority. */
export function retainPendingClarifications(
  candidates: unknown[],
  pendingRequestIds: ReadonlySet<string>,
): unknown[] {
  return candidates.filter((candidate) => {
    const requestId = clarificationId(candidate);
    return requestId !== "" && pendingRequestIds.has(requestId);
  });
}

export function resolveLiveClarifications(args: {
  clarifications: unknown[];
  resolvedClarIds: Set<string>;
  /** Whether lead.final.payload appeared (old proxy). */
  final: unknown;
  /** response_kind stamped by done (old proxy — also present while paused). */
  responseKind: "chat" | "canvas" | null;
  /** Status of the last done event (authoritative). */
  lastDoneStatus: string | null;
}): unknown[] {
  const { clarifications, resolvedClarIds, final, responseKind, lastDoneStatus } = args;
  const pausedOnClarification = lastDoneStatus === "clarification_pending";
  const turnEnded =
    !pausedOnClarification
    && (final !== undefined
      || responseKind !== null
      || (lastDoneStatus !== null && TERMINAL_DONE_STATUSES.has(lastDoneStatus)));
  if (turnEnded) return [];
  return clarifications.filter((c) => {
    const id = clarificationId(c);
    return id === "" || !resolvedClarIds.has(id);
  });
}
