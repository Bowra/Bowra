/**
 * use-proposed-plan-gate — bug 0708 ④a: tells the shell (workspace.tsx) whether
 * the CURRENT turn has a plan awaiting approval, so it can hide the bottom main
 * composer while the plan card shows its own amend input (otherwise two input
 * boxes stack — the reported bug).
 *
 * Turn-scoped on purpose: a plan parked at "proposed" in ANOTHER session's turn
 * (user navigated away without approving) must not blank the composer in the
 * session you're now looking at. plan-store is a module-level Map that is not
 * cleared on session switch, so a global "any proposed plan?" check would leak.
 *
 * Approve → status flips to "running" / cancel → dismiss removes the plan, so
 * this returns false again and the composer comes back automatically.
 *
 * Wraps useTurn + usePlan so workspace.tsx doesn't reach into turn-store /
 * plan-store directly (mirrors use-steer-acks).
 */

import { usePlan } from "./plan-store";
import { useTurn } from "./turn-store";

export function useTurnAwaitingPlanApproval(turnId: string | null | undefined): boolean {
  const turn = useTurn(turnId ?? "");
  const planBeat = turn?.beats.find((b) => b.kind === "plan");
  const planId = planBeat && planBeat.kind === "plan" ? planBeat.planId : "";
  const plan = usePlan(planId);
  return plan?.status === "proposed";
}
