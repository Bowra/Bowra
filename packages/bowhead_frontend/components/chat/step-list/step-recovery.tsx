"use client";

/**
 * Whether every failed step of a Turn was structurally recovered, from the persisted `done.step_outcome`.
 * Live, refresh and replay read the same receipt through the shared dispatcher, so a failed row's detail
 * reads the same in all three. Missing providers and missing or malformed receipts mean unknown (false).
 */
import { createContext, useContext, type ReactNode } from "react";

import { useTurn } from "@/lib/turn-store";
import type { StepOutcome } from "@/lib/turn-feedback";

const StepIssuesRecoveredContext = createContext(false);

/**
 * True only for a completed Turn whose receipt records issue steps and no unresolved one: each failed step was
 * followed by a later success of the same owner and Tool, or a later successful dispatch of the same profile
 * (`turn_step_outcome.summarize_step_outcome`). It says nothing about whether the answer met the request.
 */
export function stepIssuesAllRecovered(outcome: StepOutcome | null | undefined): boolean {
  return !!outcome && outcome.turnCompleted && outcome.issueSteps > 0 && outcome.unresolvedSteps === 0;
}

export function StepRecoveryProvider({ turnId, children }: { turnId: string; children: ReactNode }) {
  const outcome = useTurn(turnId)?.stepOutcome;
  return (
    <StepIssuesRecoveredContext.Provider value={stepIssuesAllRecovered(outcome)}>
      {children}
    </StepIssuesRecoveredContext.Provider>
  );
}

export function useStepIssuesRecovered(): boolean {
  return useContext(StepIssuesRecoveredContext);
}
