"use client";

/**
 * TurnStream — chat-redesign-2026-05-21 Phase 1.2 cutover.
 *
 * Now delegates to TurnContainer (new step-list/* surface backed by
 * lib/turn-store). The legacy V3TurnStream + reducer family was deleted
 * in Phase 1.2 Turn 2 — this file remains as the cutover seam so future
 * surface swaps can edit one wrapper instead of every Workspace caller.
 *
 * onComposerPrefill / onAttachStream are kept on the prop signature so the
 * upstream Workspace doesn't have to change in this commit. Phase 1.4 wires
 * them through (HITL resume + steer reconnect); Phase 1.2 ignores them.
 *
 * No AB flag (per `feedback_frontend_no_ab_flag`); rollback path is docker
 * image revert per `feedback_no_canary_small_userbase`.
 */

import type { TurnRunnerState } from "@/lib/types";

import { TurnContainer } from "./TurnContainer";

interface Props {
  runner: TurnRunnerState;
  /** HITL continuation — picker option selection (forwarded to TurnContainer). */
  onSend?: (text: string) => void;
  /** Continue button: resume these saved helper runs with a visible "continue" message. */
  onContinue?: (checkpointIds: string[]) => void | Promise<boolean>;
  /** Composer prefill on clarification dismiss — wired in Phase 1.4. */
  onComposerPrefill?: (originalText: string | null) => void;
  /** EventSource reattach after /resume_turn — wired in Phase 1.4. */
  onAttachStream?: (turnId: string, streamUrl: string) => void;
  /** workflow-solidify surface B: open the solidify card for a completed analysis turn. */
  onSolidify?: (turnId: string) => void;
  /** scheduled-recipe-runs S2: solidify the entire conversation (forward to TurnContainer). */
  onSolidifySession?: (sessionId: string) => void;
  /** Chat turn feedback D5: failure-card Retry for the latest turn (the composer's replay path). */
  onRetryTurn?: () => void;
  /** The replay path restores the question into the composer instead of sending (no server replay). */
  retryRestoresDraft?: boolean;
}

export function TurnStream({ runner, onSend, onContinue, onSolidify, onSolidifySession, onComposerPrefill, onRetryTurn, retryRestoresDraft }: Props) {
  // onComposerPrefill puts text back into the composer without sending it (expired clarification "Ask again",
  // failure-card "Edit and send").
  return (
    <TurnContainer
      runner={runner}
      onSend={onSend}
      onContinue={onContinue}
      onSolidify={onSolidify}
      onSolidifySession={onSolidifySession}
      onComposerPrefill={onComposerPrefill}
      onRetryTurn={onRetryTurn}
      retryRestoresDraft={retryRestoresDraft}
    />
  );
}
