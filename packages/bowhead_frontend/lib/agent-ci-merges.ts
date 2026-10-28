/**
 * CI proposals the Owner merged in this page session. The workbench lists only open proposals, so a merged
 * CI card would vanish on the next refresh; this keeps it ("Merged" with the next step) until the workbench
 * shows a candidate prepared after the merge (tech-design §6 "After acceptance").
 *
 * Module state, like the deferred dismissals: the proposals view unmounts when the Owner switches tabs.
 */
import { useSyncExternalStore } from "react";

import type { AgentChangeProposal, AgentVersionWorkbench } from "./agents-api";

/** Polling for the prepared candidate stops this long after the merge (tech-design §6 "Freshness"). */
export const CI_MERGE_POLL_WINDOW_MS = 10 * 60 * 1000;

export interface CiMerge {
  agentId: string;
  proposal: AgentChangeProposal;
  /** Epoch milliseconds of the merge. */
  mergedAt: number;
  /** The candidate on screen when the merge happened; a different one is the candidate CI prepared. */
  candidateIdAtMerge: string | null;
  /** The first candidate seen after the merge; set once, so a later release doesn't reopen the wait. */
  preparedCandidateId: string | null;
}

let merges: readonly CiMerge[] = [];
const listeners = new Set<() => void>();

function emit(next: readonly CiMerge[]) {
  merges = next;
  listeners.forEach((listener) => listener());
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function isCiProposal(proposal: Pick<AgentChangeProposal, "source_type">): boolean {
  return proposal.source_type === "git";
}

/** Records a merge with the status the apply response reported for the proposal, never an assumed one. */
export function recordCiMerge(
  agentId: string,
  proposal: AgentChangeProposal,
  status: AgentChangeProposal["status"],
  workbench: Pick<AgentVersionWorkbench, "candidate_identity">,
  now = Date.now(),
) {
  if (!isCiProposal(proposal)) return;
  emit([
    ...merges.filter((item) => !(item.agentId === agentId && item.proposal.proposal_id === proposal.proposal_id)),
    {
      agentId,
      proposal: { ...proposal, status, decided_at: new Date(now).toISOString() },
      mergedAt: now,
      candidateIdAtMerge: workbench.candidate_identity?.candidate_id ?? null,
      preparedCandidateId: null,
    },
  ]);
}

/** A candidate other than the one on screen at merge time has appeared: CI prepared it. */
function newCandidate(merge: CiMerge, workbench: Pick<AgentVersionWorkbench, "candidate_identity">): string | null {
  const candidate = workbench.candidate_identity?.candidate_id ?? null;
  return candidate !== null && candidate !== merge.candidateIdAtMerge ? candidate : null;
}

/** Records the candidate CI prepared after each of this Agent's merges; called with each workbench refresh. */
export function settleCiMerges(agentId: string, workbench: Pick<AgentVersionWorkbench, "candidate_identity">) {
  let changed = false;
  const next = merges.map((item) => {
    if (item.agentId !== agentId || item.preparedCandidateId) return item;
    const prepared = newCandidate(item, workbench);
    if (!prepared) return item;
    changed = true;
    return { ...item, preparedCandidateId: prepared };
  });
  if (changed) emit(next);
}

/** Still waiting for its candidate: none seen yet, and none on screen now. */
export function ciMergeWaiting(merge: CiMerge, workbench: Pick<AgentVersionWorkbench, "candidate_identity"> | null): boolean {
  return !merge.preparedCandidateId && (!workbench || !newCandidate(merge, workbench));
}

/** This Agent's merges, newest first. */
export function agentCiMerges(all: readonly CiMerge[], agentId: string): CiMerge[] {
  return all.filter((item) => item.agentId === agentId).sort((a, b) => b.mergedAt - a.mergedAt);
}

/** Merged CI proposals still waiting for their candidate, newest first. */
export function waitingCiMerges(all: readonly CiMerge[], agentId: string, workbench: Pick<AgentVersionWorkbench, "candidate_identity"> | null): CiMerge[] {
  return agentCiMerges(all, agentId).filter((item) => ciMergeWaiting(item, workbench));
}

export function useCiMerges(): readonly CiMerge[] {
  return useSyncExternalStore(subscribe, () => merges, () => merges);
}

/** Test helper: forget every merge. */
export function resetCiMerges() {
  emit([]);
}
