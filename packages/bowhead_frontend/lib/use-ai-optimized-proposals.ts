"use client";

/**
 * Which Coach rows in the draft came from AI 优化 (slices-2-5-tech-design §3.1 "Field chips"). The workbench lists
 * only open proposals, so an applied one is read once per proposal id through `GET …/change-proposals/{id}` and
 * remembered for the page's lifetime: a proposal's `source_ref` never changes.
 */

import { useEffect, useMemo, useState } from "react";

import { getAgentChangeProposal, type AgentChangeProposal, type AgentDraftChange } from "@/lib/agents-api";
import { isAiOptimizeProposal } from "@/lib/agent-optimize";

const known = new Map<string, boolean>();
const reading = new Map<string, Promise<boolean>>();

function cacheKey(agentId: string, proposalId: string): string {
  return `${agentId}:${proposalId}`;
}

/** Test hook: forget what earlier tests read. */
export function resetAiOptimizedProposalsForTest(): void {
  known.clear();
  reading.clear();
}

function readOnce(agentId: string, proposalId: string): Promise<boolean> {
  const key = cacheKey(agentId, proposalId);
  const cached = known.get(key);
  if (cached !== undefined) return Promise.resolve(cached);
  let pending = reading.get(key);
  if (!pending) {
    pending = getAgentChangeProposal(agentId, proposalId).then(
      (proposal) => {
        const value = isAiOptimizeProposal(proposal);
        known.set(key, value);
        return value;
      },
      // A failed read leaves the row as an ordinary Coach change; the next mount asks again.
      () => false,
    ).finally(() => reading.delete(key));
    reading.set(key, pending);
  }
  return pending;
}

/** The Coach proposal ids behind the draft's Coach rows. */
export function coachProposalIds(changes: readonly AgentDraftChange[] | undefined): string[] {
  return [...new Set((changes ?? []).map((change) => change.coach?.proposal_id).filter((id): id is string => Boolean(id)))].sort();
}

/** The ids among `proposalIds` that AI 优化 produced; proposals already on the page are read from it. */
export function useAiOptimizedProposalIds(
  agentId: string,
  proposalIds: readonly string[],
  listed: readonly AgentChangeProposal[] = [],
  enabled = true,
): ReadonlySet<string> {
  const idsKey = proposalIds.join(",");
  for (const proposal of listed) known.set(cacheKey(agentId, proposal.proposal_id), isAiOptimizeProposal(proposal));
  const [version, setVersion] = useState(0);
  useEffect(() => {
    if (!enabled || !idsKey) return undefined;
    let active = true;
    const missing = idsKey.split(",").filter((id) => !known.has(cacheKey(agentId, id)));
    if (!missing.length) return undefined;
    void Promise.all(missing.map((id) => readOnce(agentId, id))).then(() => {
      if (active) setVersion((value) => value + 1);
    });
    return () => { active = false; };
  }, [agentId, enabled, idsKey]);
  return useMemo(() => {
    void version;
    return new Set(idsKey ? idsKey.split(",").filter((id) => known.get(cacheKey(agentId, id)) === true) : []);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `version` marks a finished read
  }, [agentId, idsKey, version, listed]);
}
