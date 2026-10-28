import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { CI_MERGE_POLL_WINDOW_MS, recordCiMerge, resetCiMerges, useCiMerges, waitingCiMerges } from "./agent-ci-merges";
import type { AgentChangeProposal } from "./agents-api";

const proposal = {
  kind: "agent_change_proposal",
  schema_version: 1,
  proposal_id: "acp_ci",
  agent_id: "osa_1",
  source_type: "git",
  source_ref: "git:run:1",
  rationale: "",
  status: "open",
  row_version: 1,
  created_at: "2026-10-18T00:00:00Z",
  decided_at: null,
  operations: [],
  atomic_groups: [],
  suggested_cases: [],
  base: { release_id: null, working_copy_id: null, working_copy_row_version: null },
} as AgentChangeProposal;

afterEach(() => resetCiMerges());

describe("recordCiMerge", () => {
  it("keeps the status the apply response reported", () => {
    recordCiMerge("osa_1", proposal, "superseded", { candidate_identity: null }, 1_000);
    const { result } = renderHook(() => useCiMerges());
    expect(result.current.map((merge) => [merge.proposal.status, merge.mergedAt])).toEqual([["superseded", 1_000]]);
    expect(waitingCiMerges(result.current, "osa_1", null).map((merge) => merge.proposal.proposal_id)).toEqual(["acp_ci"]);
    expect(CI_MERGE_POLL_WINDOW_MS).toBe(10 * 60 * 1000);
  });

  it("ignores proposals that didn't come from CI", () => {
    recordCiMerge("osa_1", { ...proposal, source_type: "coach" }, "accepted", { candidate_identity: null });
    const { result } = renderHook(() => useCiMerges());
    expect(result.current).toEqual([]);
  });
});
