/**
 * 存为用例 from a trial answer (trial-tech-design §5): draft a case from the Owner's own trial Turn with the
 * existing suggestion route, then add it to the Owner's suite with the existing suite save. The points stay
 * drafts the Owner writes on 测试用例 (「去写要点」).
 */

import {
  getAgentEvalManager,
  saveAgentEvalSuite,
  suggestAgentEvalCasesFromTurns,
  type AgentEvalCase,
} from "@/lib/agents-api";

/** The server accepts up to 200 cases per suite revision. */
const MAX_SUITE_CASES = 200;
const ALIGNMENT_CASE = /^align\.[A-Za-z0-9_-]+$/;

function caseTask(value: AgentEvalCase): string {
  return (value.task ?? value.input?.task ?? "").trim();
}

/** The fields the suite save accepts, numbered `quality.N` like the suite editor (alignment keys are kept). */
function savedCase(item: AgentEvalCase, index: number): AgentEvalCase {
  return {
    case_key: ALIGNMENT_CASE.test(item.case_key) ? item.case_key : `quality.${index + 1}`,
    task: caseTask(item),
    fixture_ref: item.fixture_ref ?? {},
    expected_contract: item.expected_contract,
    scorer_config: item.scorer_config,
    effect_policy: item.effect_policy,
    valid_until: item.valid_until ?? null,
  };
}

export class TrialCaseError extends Error {
  constructor(readonly code: "agent_trial_case_empty" | "agent_trial_suite_full") {
    super(code);
  }
}

/** Adds the trial Turn's question as one case; resolves the number of cases now in the suite. */
export async function saveTrialTurnAsCase(agentId: string, turnId: string, defaultSuiteName: string): Promise<number> {
  const { cases: drafted } = await suggestAgentEvalCasesFromTurns(agentId, [turnId]);
  const added = drafted.filter((item) => caseTask(item));
  if (!added.length) throw new TrialCaseError("agent_trial_case_empty");
  const manager = await getAgentEvalManager(agentId);
  const suite = manager.suites.find((item) => item.suite_kind === "owner_quality" && item.state !== "archived");
  const existing = suite?.revision?.cases.filter((item) => caseTask(item)) ?? [];
  if (existing.length + added.length > MAX_SUITE_CASES) throw new TrialCaseError("agent_trial_suite_full");
  const cases = [...existing, ...added].map(savedCase);
  await saveAgentEvalSuite(agentId, {
    suite_id: suite?.suite_id ?? null,
    name: suite?.name ?? defaultSuiteName,
    visibility: suite?.visibility ?? "owner",
    cases,
  });
  return cases.length;
}
