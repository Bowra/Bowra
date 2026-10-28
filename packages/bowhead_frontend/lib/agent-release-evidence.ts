/**
 * What the release evidence actually shows (design: agent-release-beginner-experience §9).
 *
 * `release_gate.ready` alone says only that the fixed safety contract allows a release. Whether there
 * is evidence that the Agent answers well depends on the Owner's own cases, so the card title comes
 * from this evidence state and never claims "ready" when the Owner suite has no cases.
 */
import type { AgentEvalReleaseGate, AgentEvalSuite } from "@/lib/agents-api";

/** Evidence states that carry a result for the current target. */
const RESULT_STATES = new Set(["valid", "passed", "failed", "blocked", "unknown", "incomplete"]);

export type ReleaseEvidence =
  | { kind: "waiting" }
  | { kind: "safety_only"; safety: { passed: number; count: number } | null }
  | { kind: "not_run"; suite: AgentEvalSuite; count: number }
  | { kind: "ready"; suite: AgentEvalSuite; passed: number; count: number; at: string | null };

function runCounts(suite: AgentEvalSuite): { passed: number; count: number } | null {
  const summary = suite.evidence.run?.summary;
  if (!summary) return null;
  return { passed: Number(summary["pass@k_cases"] ?? 0), count: Number(summary.case_count ?? suite.revision?.case_count ?? 0) };
}

export function releaseEvidence(gate: AgentEvalReleaseGate, suites: AgentEvalSuite[]): ReleaseEvidence {
  if (!gate.ready) return { kind: "waiting" };
  const owner = suites.filter((suite) => suite.suite_kind === "owner_quality" && (suite.revision?.case_count ?? 0) > 0);
  if (!owner.length) {
    const platform = suites.find((suite) => suite.suite_kind === "platform_contract");
    return { kind: "safety_only", safety: platform ? runCounts(platform) : null };
  }
  const withResult = owner.find((suite) => RESULT_STATES.has(suite.evidence.state) && suite.evidence.run);
  if (withResult) {
    const counts = runCounts(withResult) ?? { passed: 0, count: withResult.revision?.case_count ?? 0 };
    const run = withResult.evidence.run;
    return { kind: "ready", suite: withResult, ...counts, at: run?.finished_at ?? run?.started_at ?? withResult.evidence.updated_at ?? null };
  }
  return { kind: "not_run", suite: owner[0], count: owner[0].revision?.case_count ?? 0 };
}
