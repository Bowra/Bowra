import { describe, expect, it } from "vitest";

import type { AgentEvalSuite } from "@/lib/agents-api";

import { releaseEvidence } from "./agent-release-evidence";

function suite(kind: AgentEvalSuite["suite_kind"], caseCount: number, state: string, passed = 0): AgentEvalSuite {
  return {
    suite_id: `aes_${kind}`,
    suite_kind: kind,
    name: kind === "owner_quality" ? "我的效果检查" : "",
    visibility: "owner",
    state: "active",
    row_version: 1,
    platform_managed: kind === "platform_contract",
    revision: { suite_revision_id: `aev_${kind}`, sequence: 1, case_count: caseCount, created_at: "2026-10-20T00:00:00Z", cases: [] },
    evidence: {
      state,
      stale_reason: null,
      updated_at: null,
      run: state === "missing" ? null : ({ run_id: "aer_1", status: "completed", finished_at: "2026-10-20T12:40:00Z", summary: { case_count: caseCount, "pass@k_cases": passed } } as unknown as AgentEvalSuite["evidence"]["run"]),
    },
  };
}

const READY = { required: true, ready: true, reason: null };

describe("releaseEvidence", () => {
  it("never calls evidence ready without Owner cases", () => {
    expect(releaseEvidence(READY, [suite("platform_contract", 1, "passed", 1)])).toEqual({ kind: "safety_only", safety: { passed: 1, count: 1 } });
    expect(releaseEvidence(READY, [suite("platform_contract", 1, "missing"), suite("owner_quality", 0, "missing")]).kind).toBe("safety_only");
  });

  it("asks to run Owner cases that have no result on the current target", () => {
    expect(releaseEvidence(READY, [suite("owner_quality", 4, "stale")])).toMatchObject({ kind: "not_run", count: 4 });
  });

  it("is ready only with a current Owner result", () => {
    expect(releaseEvidence(READY, [suite("owner_quality", 4, "passed", 4)])).toMatchObject({ kind: "ready", passed: 4, count: 4, at: "2026-10-20T12:40:00Z" });
  });

  it("keeps the waiting state while the gate is not ready", () => {
    expect(releaseEvidence({ ...READY, ready: false }, [suite("owner_quality", 4, "passed", 4)])).toEqual({ kind: "waiting" });
  });
});
